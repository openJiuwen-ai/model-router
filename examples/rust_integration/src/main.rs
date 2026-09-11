//! Rust 宿主集成示例：如何在一个 Rust 程序中接入 openjiuwen-router。
//!
//! 演示宿主侧的标准闭环：
//!
//!     构造 RouteRequest → router.route 选模型 → 宿主自己调模型 → router.report 回报
//!     失败时把模型加入 exclusions 并回报 Unavailable，下一次 route 自动换模
//!
//! 运行（在本目录执行）：
//!
//!     cargo run
//!
//! 模型调用由 mock 扮演，不发真实网络；接入真实业务时把 call_model 换成
//! 你的模型客户端即可，路由相关代码不需要改动。

use openjiuwen_runtime::{
    Feedback, Message, Outcome, RequestMetadata, RouteHint, RouteRequest, Router,
};

// 与 config/edge.toml 等价的内联 profile；也可以 Router::from_config("config/edge.toml")。
const PROFILE: &str = r#"
algorithm = "passthrough"

[state]
backend = "memory"
ttl_secs = 300
max_entries = 1024

[targets]
models = ["fast-local", "strong-cloud"]
"#;

/// 模拟模型客户端：fast-local 不可用，strong-cloud 正常响应。
fn call_model(model_id: &str, prompt: &str) -> Result<String, String> {
    match model_id {
        "fast-local" => Err(format!("{model_id} unavailable")),
        _ => Ok(format!("[{model_id}] {prompt}")),
    }
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let router = Router::from_toml(PROFILE)?;
    println!("algorithm: {}", router.algorithm_name());

    // 稳定的会话标识：route 与 report 必须使用同一组，否则状态无法闭环。
    let session_id = "demo-session";
    let agent_id = "rust-host-example";
    let prompt = "hello";

    let mut exclusions: Vec<String> = Vec::new();
    for _ in 0..4 {
        let request = RouteRequest {
            messages: vec![Message {
                role: "user".into(),
                content: prompt.into(),
            }],
            metadata: RequestMetadata {
                session_id: Some(session_id.into()),
                agent_id: Some(agent_id.into()),
            },
            exclusions: exclusions.clone(),
        };

        let decision = router.route(&request, &RouteHint::default())?;
        println!(
            "route → {} ({})",
            decision.selected_model_id, decision.reasoning
        );

        match call_model(&decision.selected_model_id, prompt) {
            Ok(reply) => {
                let mut feedback = Feedback::ok(request.routing_key(), &decision.selected_model_id, 1);
                feedback.decision_id = decision.decision_id.clone();
                router.report(feedback);
                println!("reply: {reply}");
                return Ok(());
            }
            Err(reason) => {
                println!(
                    "{} failed: {reason}, report Unavailable",
                    decision.selected_model_id
                );
                let mut feedback = Feedback::ok(request.routing_key(), &decision.selected_model_id, 1);
                feedback.decision_id = decision.decision_id.clone();
                feedback.call.as_mut().unwrap().outcome = Outcome::Unavailable;
                router.report(feedback);
                exclusions.push(decision.selected_model_id.clone());
            }
        }
    }

    Err("no available target after retries".into())
}
