//! 最小 ReAct 宿主：验证「决策与执行分离」。
//!
//! 循环是 Thought → Action → Observation → Final Answer。
//! 每一次模型调用前问 [`Router::route`]，调用后 [`Router::report`]。
//! 模型本身由脚本化 mock 扮演，不发真实网络。

use openjiuwen_runtime::{
    Decision, Feedback, Message, Outcome, RequestMetadata, RouteHint, RouteRequest, Router,
    RouterError, RoutingKey,
};

const PROFILE: &str = r#"
algorithm = "passthrough"
[state]
backend = "memory"
[targets]
models = ["fast-local", "strong-cloud"]
"#;

/// 脚本化后端：`fast-local` 模拟不可用；`strong-cloud` 按 ReAct 剧本回复。
struct MockBackend;

impl MockBackend {
    fn invoke(&self, model: &str, prompt: &str) -> Result<String, &'static str> {
        if model == "fast-local" {
            return Err("unavailable");
        }
        if prompt.contains("Observation:") {
            Ok("Thought: I have the result.\nFinal Answer: 42".into())
        } else {
            Ok("Thought: I should calculate.\nAction: calc[21*2]".into())
        }
    }
}

enum LlmTurn {
    Act {
        thought: String,
        tool: String,
        input: String,
    },
    Finish {
        thought: String,
        answer: String,
    },
}

// 解析模型输出。
fn parse_turn(text: &str) -> LlmTurn {
    // 返回的是 LlmTurn 类型。
    // 提取 Thought。
    let thought = line_after(text, "Thought:").unwrap_or_default();
    // 提取 Final Answer。
    if let Some(answer) = line_after(text, "Final Answer:") {
        return LlmTurn::Finish { thought, answer };
    }
    // 提取 Action。
    let action: String =
        line_after(text, "Action:").expect("mock llm must emit Action or Final Answer");
    // 分割工具和输入。
    let (tool, input) = split_tool(&action);
    LlmTurn::Act {
        thought,
        tool,
        input,
    }
}

fn line_after(text: &str, prefix: &str) -> Option<String> {
    text.lines()
        .find_map(|line| line.trim().strip_prefix(prefix))
        .map(|s| s.trim().to_string())
}

fn split_tool(action: &str) -> (String, String) {
    match action.split_once('[') {
        Some((name, rest)) => (
            name.trim().to_string(),
            rest.trim().trim_end_matches(']').to_string(),
        ),
        None => (action.to_string(), String::new()),
    }
}

fn run_tool(name: &str, input: &str) -> String {
    assert_eq!(name, "calc", "this skeleton only ships a calc tool");
    let (lhs, rhs) = input.split_once('*').expect("calc input like 21*2");
    let a: i64 = lhs.trim().parse().unwrap();
    let b: i64 = rhs.trim().parse().unwrap();
    (a * b).to_string()
}

/// 宿主侧最小 ReActAgent：路由选模 → 自己调模型 → 自己跑工具。
struct ReActAgent {
    router: Router,
    backend: MockBackend,
    session_id: String,
    agent_id: String,
}

impl ReActAgent {
    fn new(router: Router) -> Self {
        Self {
            router,
            backend: MockBackend,
            session_id: "sess-react".into(),
            agent_id: "react-agent".into(),
        }
    }

    // 获取路由键。
    fn routing_key(&self) -> RoutingKey {
        RoutingKey {
            session_id: self.session_id.clone(),
            agent_id: self.agent_id.clone(),
        }
    }

    // 路由选模。
    fn route(&self, question: &str, trace: &mut Vec<String>) -> Result<Decision, RouterError> {
        let req = RouteRequest {
            messages: vec![Message {
                role: "user".into(),
                content: question.into(),
                tool_calls: Vec::new(),
            }],
            metadata: RequestMetadata {
                session_id: Some(self.session_id.clone()),
                agent_id: Some(self.agent_id.clone()),
            },
            exclusions: Vec::new(),
        };
        // 路由选模。
        let decision: Decision = self.router.route(&req, &RouteHint::default())?;
        // 记录选中模型id。
        trace.push(decision.selected_model_id.clone());
        // 打印选中模型id和推理原因。
        println!(
            "  route → {} ({})",
            decision.selected_model_id, decision.reasoning
        );
        // 返回决策。
        Ok(decision)
    }

    // 上报反馈。把本次 `route` 返回的 `route_id` 带回，便于 state 把 query 与 report 对上。
    fn report(&self, decision: &Decision, outcome: Outcome) {
        let mut feedback = Feedback::ok(self.routing_key(), &decision.selected_model_id, 1);
        feedback.route_id = decision.route_id.clone();
        feedback.call.as_mut().unwrap().outcome = outcome;
        self.router.report(feedback);
    }

    /// 调模型：失败则 report Unavailable，下一轮 snapshot 会排除该目标。
    fn call_model(
        &self,
        prompt: &str,
        trace: &mut Vec<String>,
    ) -> Result<(Decision, String), RouterError> {
        for _ in 0..4 {
            // 最多4次尝试。
            let decision = self.route(prompt, trace)?;
            match self.backend.invoke(&decision.selected_model_id, prompt) {
                // 调用模型。
                Ok(text) => {
                    // 调用成功。
                    self.report(&decision, Outcome::Ok); // 上报反馈。
                    return Ok((decision, text));
                }
                Err(reason) => {
                    println!(
                        "  {} failed ({reason}), report Unavailable",
                        decision.selected_model_id
                    );
                    self.report(&decision, Outcome::Unavailable); // 上报反馈。
                }
            }
        }
        Err(RouterError::NoTarget)
    }

    // 运行 ReAct 循环。
    fn run(&self, question: &str) -> Result<(String, Vec<String>), RouterError> {
        let mut prompt: String = format!("Question: {question}\n");
        let mut trace = Vec::new();
        println!("ReAct: {question}");

        for step in 1..=4 {
            // 最多4步。
            println!("step {step}");
            let (_decision, text) = self.call_model(&prompt, &mut trace)?;
            prompt.push_str(&text);
            prompt.push('\n');

            match parse_turn(&text) {
                LlmTurn::Finish { thought, answer } => {
                    println!("  thought: {thought}");
                    println!("  final: {answer}");
                    return Ok((answer, trace));
                }
                LlmTurn::Act {
                    thought,
                    tool,
                    input,
                } => {
                    let observation = run_tool(&tool, &input);
                    println!("  thought: {thought}");
                    println!("  action: {tool}[{input}] → {observation}");
                    prompt.push_str(&format!("Observation: {observation}\n"));
                }
            }
        }
        Err(RouterError::Algorithm("max ReAct steps exceeded".into()))
    }
}

#[test]
fn react_agent_routes_retries_and_answers() {
    let router: Router = Router::from_toml(PROFILE).expect("assemble router");
    assert_eq!(router.algorithm_name(), "passthrough");

    let agent: ReActAgent = ReActAgent::new(router);
    let (answer, trace) = agent.run("What is 21 * 2?").expect("react loop");

    assert_eq!(answer, "42");
    assert_eq!(
        trace,
        vec![
            "fast-local",   // passthrough 首选；mock 不可用
            "strong-cloud", // report Unavailable 后 state 排除，第二次选中
            "strong-cloud", // 第二轮 Thought/Final 仍走可用模型
        ]
    );
}
