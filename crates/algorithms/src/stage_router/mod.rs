use std::collections::VecDeque;

use openjiuwen_protocol::{Decision, RouteRequest, RouterError, ToolCall};

use crate::{AlgorithmProvider, RouteContext};

const SOFT_FAILURE: f64 = 0.3;
const HARD_FAILURE: f64 = 0.7;
const CRITICAL_FAILURE: f64 = 1.0;
const SCORE_GAIN: f64 = 5.0;
const EVIDENCE_WEIGHT: f64 = 0.10;
const COMPACTION_MARKER: &str = "session is being continued";

#[derive(Clone, Copy)]
struct StagePolicy {
    history_window: usize,
    confidence_floor: f64,
    stall_depth: usize,
}

const DEFAULT_POLICY: StagePolicy = StagePolicy {
    history_window: 3,
    confidence_floor: 0.5,
    stall_depth: 8,
};

struct FailureSignature {
    weight: f64,
    fragments: &'static [&'static str],
}

const FAILURE_SIGNATURES: &[FailureSignature] = &[
    FailureSignature {
        weight: CRITICAL_FAILURE,
        fragments: &["out of memory", "memoryerror", "cannot allocate memory"],
    },
    FailureSignature {
        weight: CRITICAL_FAILURE,
        fragments: &[
            "connection refused",
            "connectionrefusederror",
            "econnrefused",
        ],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["traceback (most recent call last)"],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["modulenotfounderror:", "importerror:", "no module named "],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["command not found", "not found\n", "/usr/bin/env: "],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["assertionerror"],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["valueerror:"],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &["syntaxerror:"],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &[
            "timed out",
            "timeouterror",
            "timeout expired",
            "deadline exceeded",
        ],
    },
    FailureSignature {
        weight: HARD_FAILURE,
        fragments: &[
            "filenotfounderror:",
            "no such file or directory",
            "file does not exist",
        ],
    },
    FailureSignature {
        weight: SOFT_FAILURE,
        fragments: &[
            "exit code 1",
            "exit code 2",
            "exit status 1",
            "returned non-zero",
            "exited with code",
        ],
    },
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

        let snapshot = observe_history(request, DEFAULT_POLICY);
        let assessment = assess_stage(&snapshot, DEFAULT_POLICY);
        let selected = match assessment.tier {
            ModelTier::Efficient => efficient,
            ModelTier::Capable => capable,
        };

        Ok(Decision::answer(
            selected,
            format!(
                "stage_router: source={}, score={:.3}, confidence={:.3}",
                assessment.basis.label(),
                assessment.score,
                assessment.confidence
            ),
        ))
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct ActivityCounts {
    writes: usize,
    edits: usize,
    reads: usize,
    plans: usize,
}

impl ActivityCounts {
    fn record(&mut self, operation: OperationKind) {
        let counter = match operation {
            OperationKind::Write => &mut self.writes,
            OperationKind::Edit => &mut self.edits,
            OperationKind::Read => &mut self.reads,
            OperationKind::Plan => &mut self.plans,
            OperationKind::Other => return,
        };
        *counter = counter.saturating_add(1);
    }

    fn production(self) -> usize {
        self.writes.saturating_add(self.edits)
    }

    fn total(self) -> usize {
        self.production()
            .saturating_add(self.reads)
            .saturating_add(self.plans)
    }

    fn is_investigating(self) -> bool {
        self.reads != 0 || self.plans != 0
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct HistorySnapshot {
    failure_pressure: f64,
    activity: ActivityCounts,
    clean_test_run: bool,
    conversation_depth: usize,
    context_was_compacted: bool,
}

impl HistorySnapshot {
    fn requires_capable_model(self) -> bool {
        self.context_was_compacted || self.failure_pressure >= CRITICAL_FAILURE
    }

    fn represents_completed_work(self) -> bool {
        self.clean_test_run && self.activity.production() != 0 && self.failure_pressure <= 0.0
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct EvidenceVector {
    failure: f64,
    stalled: f64,
    investigation: f64,
    delivery_ratio: f64,
}

impl EvidenceVector {
    fn from_snapshot(snapshot: &HistorySnapshot, policy: StagePolicy) -> Self {
        let activity = snapshot.activity;
        let has_no_output = activity.production() == 0;
        let is_deep = snapshot.conversation_depth >= policy.stall_depth;
        let is_investigating = activity.is_investigating();

        Self {
            failure: snapshot.failure_pressure,
            stalled: bool_as_score(is_deep && has_no_output && !is_investigating),
            investigation: bool_as_score(is_deep && has_no_output && is_investigating),
            delivery_ratio: ratio(activity.production(), activity.total()),
        }
    }

    fn signed_score(self) -> f64 {
        let combined =
            self.failure / HARD_FAILURE + self.stalled + self.investigation - self.delivery_ratio;
        (SCORE_GAIN * EVIDENCE_WEIGHT * combined).tanh()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OperationKind {
    Write,
    Edit,
    Read,
    Plan,
    Other,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ModelTier {
    Efficient,
    Capable,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum DecisionBasis {
    SafetyOverride,
    VerifiedProgress,
    EvidenceScore,
    DefaultTier,
}

impl DecisionBasis {
    fn label(self) -> &'static str {
        match self {
            Self::SafetyOverride => "override",
            Self::VerifiedProgress => "tests_passed",
            Self::EvidenceScore => "dimensions",
            Self::DefaultTier => "fall_open",
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct StageAssessment {
    tier: ModelTier,
    basis: DecisionBasis,
    score: f64,
    confidence: f64,
}

fn assess_stage(snapshot: &HistorySnapshot, policy: StagePolicy) -> StageAssessment {
    if snapshot.requires_capable_model() {
        return StageAssessment {
            tier: ModelTier::Capable,
            basis: DecisionBasis::SafetyOverride,
            score: 0.0,
            confidence: 1.0,
        };
    }
    if snapshot.represents_completed_work() {
        return StageAssessment {
            tier: ModelTier::Efficient,
            basis: DecisionBasis::VerifiedProgress,
            score: 0.0,
            confidence: 0.0,
        };
    }

    let score = EvidenceVector::from_snapshot(snapshot, policy).signed_score();
    let confidence = score.abs();
    let (tier, basis) = if confidence >= policy.confidence_floor {
        (
            if score > 0.0 {
                ModelTier::Capable
            } else {
                ModelTier::Efficient
            },
            DecisionBasis::EvidenceScore,
        )
    } else {
        (ModelTier::Efficient, DecisionBasis::DefaultTier)
    };

    StageAssessment {
        tier,
        basis,
        score,
        confidence,
    }
}

fn observe_history(request: &RouteRequest, policy: StagePolicy) -> HistorySnapshot {
    let mut recent_results = VecDeque::with_capacity(policy.history_window.max(1));
    let mut recent_operations = VecDeque::with_capacity(policy.history_window);
    let mut conversation_depth: usize = 0;
    let mut context_was_compacted = false;

    for message in &request.messages {
        let role = message.role.to_ascii_lowercase();
        if role != "system" && role != "developer" {
            conversation_depth = conversation_depth.saturating_add(1);
            context_was_compacted |= message
                .content
                .to_ascii_lowercase()
                .contains(COMPACTION_MARKER);
        }
        if role == "tool" && !message.content.is_empty() {
            retain_latest(
                &mut recent_results,
                message.content.as_str(),
                policy.history_window.max(1),
            );
        }
        if role == "assistant" {
            for call in &message.tool_calls {
                retain_latest(
                    &mut recent_operations,
                    operation_kind(call),
                    policy.history_window,
                );
            }
        }
    }

    let failure_pressure = recent_results
        .iter()
        .map(|result| failure_weight(result))
        .fold(0.0, f64::max);
    let clean_test_run = recent_results
        .iter()
        .any(|result| is_clean_test_result(result));
    let mut activity = ActivityCounts::default();
    for operation in recent_operations {
        activity.record(operation);
    }

    HistorySnapshot {
        failure_pressure,
        activity,
        clean_test_run,
        conversation_depth,
        context_was_compacted,
    }
}

fn retain_latest<T>(items: &mut VecDeque<T>, item: T, limit: usize) {
    if limit == 0 {
        return;
    }
    if items.len() == limit {
        items.pop_front();
    }
    items.push_back(item);
}

fn bool_as_score(value: bool) -> f64 {
    if value {
        1.0
    } else {
        0.0
    }
}

fn ratio(numerator: usize, denominator: usize) -> f64 {
    if denominator == 0 {
        0.0
    } else {
        numerator as f64 / denominator as f64
    }
}

fn failure_weight(text: &str) -> f64 {
    let normalized = text.to_ascii_lowercase();
    FAILURE_SIGNATURES
        .iter()
        .filter(|signature| contains_any(&normalized, signature.fragments))
        .map(|signature| signature.weight)
        .fold(0.0, f64::max)
}

fn operation_kind(call: &ToolCall) -> OperationKind {
    let name = call.name.to_ascii_lowercase();
    if WRITE_TOOLS.contains(&name.as_str()) {
        return OperationKind::Write;
    }
    if EDIT_TOOLS.contains(&name.as_str()) {
        return OperationKind::Edit;
    }
    if READ_TOOLS.contains(&name.as_str()) {
        return OperationKind::Read;
    }
    if PLAN_TOOLS.contains(&name.as_str()) {
        return OperationKind::Plan;
    }
    if SHELL_TOOLS.contains(&name.as_str()) {
        let command = call
            .command
            .as_deref()
            .unwrap_or_default()
            .to_ascii_lowercase();
        for (patterns, kind) in [
            (SHELL_WRITE, OperationKind::Write),
            (SHELL_EDIT, OperationKind::Edit),
            (SHELL_READ, OperationKind::Read),
        ] {
            if contains_any(&command, patterns) {
                return kind;
            }
        }
    }
    OperationKind::Other
}

fn contains_any(text: &str, candidates: &[&str]) -> bool {
    candidates.iter().any(|candidate| text.contains(candidate))
}

fn is_clean_test_result(text: &str) -> bool {
    let normalized = text.to_ascii_lowercase();
    contains_any(&normalized, TEST_PASS_PHRASES)
        && !contains_any(&normalized, TEST_FAILURE_LITERAL)
        && !has_counted_failure(&normalized)
}

fn has_counted_failure(text: &str) -> bool {
    NUMERIC_FAILURE_KEYWORDS.iter().any(|keyword| {
        text.match_indices(keyword).any(|(keyword_start, _)| {
            let Some(keyword_end) = keyword_start.checked_add(keyword.len()) else {
                return false;
            };
            if text[keyword_end..]
                .chars()
                .next()
                .is_some_and(char::is_alphanumeric)
            {
                return false;
            }
            count_immediately_before(text, keyword_start).is_some_and(|count| count != 0)
        })
    })
}

fn count_immediately_before(text: &str, boundary: usize) -> Option<u64> {
    let digits_reversed: String = text[..boundary]
        .trim_end()
        .chars()
        .rev()
        .take_while(char::is_ascii_digit)
        .collect();
    if digits_reversed.is_empty() {
        return None;
    }
    digits_reversed
        .chars()
        .rev()
        .collect::<String>()
        .parse()
        .ok()
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
    fn context_compaction_overrides_to_capable() {
        let decision = route(vec![message(
            "user",
            "This session is being continued from an earlier context.",
        )]);
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
    fn zero_failures_still_count_as_a_clean_test_run() {
        assert!(is_clean_test_result("test result: ok. 8 passed; 0 failed"));
    }

    #[test]
    fn history_observer_keeps_only_the_configured_operation_window() {
        let request = RouteRequest {
            messages: vec![
                tool_call("Read", None),
                tool_call("Edit", None),
                tool_call("Write", None),
                tool_call("TodoWrite", None),
            ],
            ..RouteRequest::default()
        };

        let snapshot = observe_history(&request, DEFAULT_POLICY);
        assert_eq!(snapshot.activity.reads, 0);
        assert_eq!(snapshot.activity.edits, 1);
        assert_eq!(snapshot.activity.writes, 1);
        assert_eq!(snapshot.activity.plans, 1);
    }

    #[test]
    fn system_and_developer_messages_do_not_inflate_depth() {
        let request = RouteRequest {
            messages: vec![
                message("system", "system"),
                message("developer", "developer"),
                message("user", "task"),
            ],
            ..RouteRequest::default()
        };

        assert_eq!(
            observe_history(&request, DEFAULT_POLICY).conversation_depth,
            1
        );
    }

    #[test]
    fn one_available_target_is_always_selected() {
        let decision = StageRouter
            .decide(&RouteRequest::default(), &context(&["capable"]))
            .expect("single target should be selected");
        assert_eq!(decision.selected_model_id, "capable");
    }
}
