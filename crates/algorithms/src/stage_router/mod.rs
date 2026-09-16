// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES.
// SPDX-License-Identifier: Apache-2.0

//! Signal-only Stage Router adapted from Switchyard's rule-based implementation.
//!
//! The configured target order is `efficient`, then `capable`. The router keeps
//! the deterministic signal rules and deliberately omits telemetry, request
//! mutation, and the optional LLM-classifier fallback.

use openjiuwen_protocol::{Decision, RouteRequest, RouterError, ToolCall};

use crate::{AlgorithmProvider, RouteContext};

const SOFT: f64 = 0.3;
const HARD: f64 = 0.7;
const CRITICAL: f64 = 1.0;
const RECENT_WINDOW: usize = 3;
const CONFIDENCE_THRESHOLD: f64 = 0.5;
const STALL_MIN_TURN_DEPTH: usize = 8;
const SCORE_GAIN: f64 = 5.0;
const SIGNAL_UNIT: f64 = 0.10;
const COMPACTION_MARKER: &str = "session is being continued";

const ERROR_PATTERNS: &[(f64, &[&str])] = &[
    (
        CRITICAL,
        &["out of memory", "memoryerror", "cannot allocate memory"],
    ),
    (
        CRITICAL,
        &[
            "connection refused",
            "connectionrefusederror",
            "econnrefused",
        ],
    ),
    (HARD, &["traceback (most recent call last)"]),
    (
        HARD,
        &["modulenotfounderror:", "importerror:", "no module named "],
    ),
    (
        HARD,
        &["command not found", "not found\n", "/usr/bin/env: "],
    ),
    (HARD, &["assertionerror"]),
    (HARD, &["valueerror:"]),
    (HARD, &["syntaxerror:"]),
    (
        HARD,
        &[
            "timed out",
            "timeouterror",
            "timeout expired",
            "deadline exceeded",
        ],
    ),
    (
        HARD,
        &[
            "filenotfounderror:",
            "no such file or directory",
            "file does not exist",
        ],
    ),
    (
        SOFT,
        &[
            "exit code 1",
            "exit code 2",
            "exit status 1",
            "returned non-zero",
            "exited with code",
        ],
    ),
];

const EDIT_TOOLS: &[&str] = &[
    "edit",
    "multiedit",
    "notebookedit",
    "str_replace",
    "str_replace_based_edit_tool",
    "text_editor",
    "patch",
];
const WRITE_TOOLS: &[&str] = &["write", "create_file", "new_file", "write_file"];
const READ_TOOLS: &[&str] = &["read", "view", "read_file", "search_files"];
const PLAN_TOOLS: &[&str] = &["todowrite", "todo_write", "todo", "update_plan"];
const SHELL_TOOLS: &[&str] = &[
    "bash",
    "shell_command",
    "shell",
    "local_shell_call",
    "terminal",
];
const SHELL_WRITE: &[&str] = &[
    "cat >",
    "cat >>",
    "echo >",
    "echo >>",
    "tee ",
    "printf >",
    "printf >>",
    "> /",
    ">> /",
    "<< 'eof'",
    "<<eof",
    "<<'eof'",
    "<< eof",
];
const SHELL_EDIT: &[&str] = &[
    "sed -i",
    "sed --in-place",
    "awk -i inplace",
    "awk 'inplace=1'",
    "patch ",
    "patch -p",
    "perl -i",
    "perl -p -i",
    "perl -pi",
];
const SHELL_READ: &[&str] = &[
    "cat /", "cat ./", "cat ../", "grep ", "ls ", "ls -", "find ", "head ", "tail ", "wc ",
    "diff ", "which ", "ps ", "df ", "du ", "stat ", "file ", "less ", "more ",
];
const TEST_PASS_PHRASES: &[&str] = &[
    " passed",
    "passed in",
    "tests passed",
    "all tests passed",
    "test ok",
    "test result: ok",
    "passed.\n",
    "tests pass",
    "\nok ",
    "✓ ",
];
const TEST_FAILURE_LITERAL: &[&str] = &["✗ ", "fatal:", "assertionerror", "error:"];
const NUMERIC_FAILURE_KEYWORDS: &[&str] = &["failed", "failure", "failures", "errors", "error"];

/// Stateless default Stage Router: efficient-first and signal-only fall-open.
pub struct StageRouter;

impl AlgorithmProvider for StageRouter {
    fn name(&self) -> &str {
        "stage_router"
    }

    fn decide(&self, request: &RouteRequest, ctx: &RouteContext) -> Result<Decision, RouterError> {
        let efficient = ctx.targets.first().ok_or(RouterError::NoTarget)?;
        let Some(capable) = ctx.targets.models.get(1).map(String::as_str) else {
            return Ok(Decision::answer(
                efficient,
                "stage_router: only available target",
            ));
        };

        let signals = extract_signals(request);
        let dimensions = dimensions(&signals);
        let (target, source, score, confidence) =
            if signals.compacted || signals.severity >= CRITICAL {
                (capable, "override", 0.0, 1.0)
            } else if signals.tests_passed
                && signals.recent_write_count + signals.recent_edit_count >= 1
                && signals.severity <= 0.0
            {
                (efficient, "tests_passed", 0.0, 0.0)
            } else {
                let score = score(dimensions);
                let confidence = score.abs();
                if confidence >= CONFIDENCE_THRESHOLD {
                    let target = if score > 0.0 { capable } else { efficient };
                    (target, "dimensions", score, confidence)
                } else {
                    (efficient, "fall_open", score, confidence)
                }
            };

        Ok(Decision::answer(
            target,
            format!("stage_router: source={source}, score={score:.3}, confidence={confidence:.3}"),
        ))
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct ToolSignals {
    severity: f64,
    recent_edit_count: usize,
    recent_write_count: usize,
    recent_read_count: usize,
    recent_plan_count: usize,
    tests_passed: bool,
    turn_depth: usize,
    compacted: bool,
}

#[derive(Clone, Copy)]
struct Dimensions {
    severity: f64,
    spinning: f64,
    exploring: f64,
    production_intensity: f64,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum ToolCategory {
    Write,
    Edit,
    Read,
    Plan,
    Other,
}

fn extract_signals(request: &RouteRequest) -> ToolSignals {
    let mut tool_results = Vec::new();
    let mut calls = Vec::new();
    let mut turn_depth = 0;
    let mut compacted = false;

    for message in &request.messages {
        let role = message.role.to_ascii_lowercase();
        if role != "system" && role != "developer" {
            turn_depth += 1;
            compacted |= message
                .content
                .to_ascii_lowercase()
                .contains(COMPACTION_MARKER);
        }
        if role == "tool" && !message.content.is_empty() {
            tool_results.push(message.content.as_str());
        }
        if role == "assistant" {
            calls.extend(message.tool_calls.iter());
        }
    }

    let severity = tool_results
        .iter()
        .rev()
        .take(RECENT_WINDOW.max(1))
        .map(|text| classify_severity(text))
        .fold(0.0, f64::max);
    let recent_categories: Vec<_> = calls
        .iter()
        .rev()
        .take(RECENT_WINDOW)
        .map(|call| classify_tool_call(call))
        .collect();

    ToolSignals {
        severity,
        recent_edit_count: count(&recent_categories, ToolCategory::Edit),
        recent_write_count: count(&recent_categories, ToolCategory::Write),
        recent_read_count: count(&recent_categories, ToolCategory::Read),
        recent_plan_count: count(&recent_categories, ToolCategory::Plan),
        tests_passed: detect_tests_passed(&tool_results),
        turn_depth,
        compacted,
    }
}

fn count(categories: &[ToolCategory], expected: ToolCategory) -> usize {
    categories.iter().filter(|item| **item == expected).count()
}

fn dimensions(signals: &ToolSignals) -> Dimensions {
    let recent_ops = signals.recent_write_count
        + signals.recent_edit_count
        + signals.recent_read_count
        + signals.recent_plan_count;
    let no_production = signals.recent_write_count == 0 && signals.recent_edit_count == 0;
    let investigating = signals.recent_read_count >= 1 || signals.recent_plan_count >= 1;
    let deep_enough = signals.turn_depth >= STALL_MIN_TURN_DEPTH;

    Dimensions {
        severity: signals.severity,
        spinning: if deep_enough && no_production && !investigating {
            1.0
        } else {
            0.0
        },
        exploring: if deep_enough && no_production && investigating {
            1.0
        } else {
            0.0
        },
        production_intensity: if recent_ops == 0 {
            0.0
        } else {
            (signals.recent_write_count + signals.recent_edit_count) as f64 / recent_ops as f64
        },
    }
}

fn score(dimensions: Dimensions) -> f64 {
    let raw = SIGNAL_UNIT
        * (dimensions.severity / HARD + dimensions.spinning + dimensions.exploring
            - dimensions.production_intensity);
    (SCORE_GAIN * raw).tanh()
}

fn classify_severity(text: &str) -> f64 {
    let lower = text.to_ascii_lowercase();
    ERROR_PATTERNS
        .iter()
        .filter(|(_, patterns)| patterns.iter().any(|pattern| lower.contains(pattern)))
        .map(|(severity, _)| *severity)
        .fold(0.0, f64::max)
}

fn classify_tool_call(call: &ToolCall) -> ToolCategory {
    let name = call.name.to_ascii_lowercase();
    if WRITE_TOOLS.contains(&name.as_str()) {
        return ToolCategory::Write;
    }
    if EDIT_TOOLS.contains(&name.as_str()) {
        return ToolCategory::Edit;
    }
    if READ_TOOLS.contains(&name.as_str()) {
        return ToolCategory::Read;
    }
    if PLAN_TOOLS.contains(&name.as_str()) {
        return ToolCategory::Plan;
    }
    if SHELL_TOOLS.contains(&name.as_str()) {
        let command = call
            .command
            .as_deref()
            .unwrap_or_default()
            .to_ascii_lowercase();
        if SHELL_WRITE.iter().any(|pattern| command.contains(pattern)) {
            return ToolCategory::Write;
        }
        if SHELL_EDIT.iter().any(|pattern| command.contains(pattern)) {
            return ToolCategory::Edit;
        }
        if SHELL_READ.iter().any(|pattern| command.contains(pattern)) {
            return ToolCategory::Read;
        }
    }
    ToolCategory::Other
}

fn detect_tests_passed(tool_results: &[&str]) -> bool {
    tool_results
        .iter()
        .rev()
        .take(RECENT_WINDOW.max(1))
        .any(|text| {
            let lower = text.to_ascii_lowercase();
            TEST_PASS_PHRASES
                .iter()
                .any(|phrase| lower.contains(phrase))
                && !TEST_FAILURE_LITERAL
                    .iter()
                    .any(|phrase| lower.contains(phrase))
                && !has_nonzero_failure_count(&lower)
        })
}

fn has_nonzero_failure_count(text: &str) -> bool {
    NUMERIC_FAILURE_KEYWORDS.iter().any(|keyword| {
        text.match_indices(keyword).any(|(start, _)| {
            let end = start + keyword.len();
            if text[end..]
                .chars()
                .next()
                .is_some_and(char::is_alphanumeric)
            {
                return false;
            }
            let digits: String = text[..start]
                .trim_end()
                .chars()
                .rev()
                .take_while(char::is_ascii_digit)
                .collect::<String>()
                .chars()
                .rev()
                .collect();
            digits.parse::<u64>().is_ok_and(|count| count != 0)
        })
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use openjiuwen_protocol::{Message, StateView, TargetSet};

    fn context(models: &[&str]) -> RouteContext {
        RouteContext {
            targets: TargetSet::new(models.iter().copied()),
            view: StateView::empty(),
            retrieved: Vec::new(),
            seed: 0,
        }
    }

    fn message(role: &str, content: &str) -> Message {
        Message {
            role: role.into(),
            content: content.into(),
            ..Message::default()
        }
    }

    fn tool_call(name: &str, command: Option<&str>) -> Message {
        Message {
            role: "assistant".into(),
            tool_calls: vec![ToolCall {
                name: name.into(),
                command: command.map(str::to_owned),
            }],
            ..Message::default()
        }
    }

    fn route(messages: Vec<Message>) -> Decision {
        StageRouter
            .decide(
                &RouteRequest {
                    messages,
                    ..RouteRequest::default()
                },
                &context(&["efficient", "capable"]),
            )
            .expect("stage routing should succeed")
    }

    #[test]
    fn empty_history_falls_open_to_efficient() {
        let decision = route(vec![message("user", "hello")]);
        assert_eq!(decision.selected_model_id, "efficient");
        assert!(decision.reasoning.contains("source=fall_open"));
    }

    #[test]
    fn critical_error_overrides_to_capable() {
        let decision = route(vec![
            message("user", "fix it"),
            tool_call("Bash", None),
            message("tool", "fatal: out of memory"),
        ]);
        assert_eq!(decision.selected_model_id, "capable");
        assert!(decision.reasoning.contains("source=override"));
    }

    #[test]
    fn correlated_error_and_exploration_route_to_capable() {
        let mut messages = vec![message("system", "instructions")];
        messages.extend((0..8).map(|index| message("user", &format!("turn {index}"))));
        messages.push(tool_call("Bash", Some("cat /tmp/result")));
        messages.push(message("tool", "Traceback (most recent call last)"));

        let decision = route(messages);
        assert_eq!(decision.selected_model_id, "capable");
        assert!(decision.reasoning.contains("source=dimensions"));
    }

    #[test]
    fn passed_tests_after_edit_route_to_efficient() {
        let decision = route(vec![
            message("user", "implement"),
            tool_call("Edit", None),
            message("tool", "25 passed in 1.2s"),
        ]);
        assert_eq!(decision.selected_model_id, "efficient");
        assert!(decision.reasoning.contains("source=tests_passed"));
    }

    #[test]
    fn failures_do_not_trigger_tests_passed() {
        let decision = route(vec![
            message("user", "implement"),
            tool_call("Edit", None),
            message("tool", "2 failed, 5 passed"),
        ]);
        assert!(decision.reasoning.contains("source=fall_open"));
    }

    #[test]
    fn one_available_target_is_always_selected() {
        let decision = StageRouter
            .decide(&RouteRequest::default(), &context(&["capable"]))
            .expect("single target should be selected");
        assert_eq!(decision.selected_model_id, "capable");
    }
}
