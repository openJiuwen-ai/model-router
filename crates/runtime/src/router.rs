//! Router 门面：from_config / route / report。

use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use openjiuwen_algorithms::AlgorithmProvider;
use openjiuwen_protocol::{
    Decision, Feedback, FeedbackError, ModelSelection, RouteHint, RouteRequest, RouterError,
    TargetSet,
};
use openjiuwen_state::{test_state as backends, StateProvider};

use crate::config::RouterProfile;
use crate::decide_loop;
use crate::registry;
use crate::RouterProvider;

/// 模型切换时的 KV cache 协调回调。骨架仅保存，不触发。
pub trait KvCacheCoordinator: Send + Sync {
    fn on_switch(&self, from: &str, to: &str);
}

/// 已装配的路由实例。运行期算法槽与 state 槽各生效一个。
pub struct Router {
    algorithm: Box<dyn AlgorithmProvider>,
    state: Arc<dyn StateProvider>,
    targets: TargetSet,
    seed: AtomicU64,
    #[allow(dead_code)]
    kv_coordinator: Option<Box<dyn KvCacheCoordinator>>,
}

impl Router {
    pub fn from_config(path: impl AsRef<Path>) -> Result<Self, RouterError> {
        Self::from_profile(RouterProfile::from_path(path)?)
    }

    pub fn from_toml(text: &str) -> Result<Self, RouterError> {
        Self::from_profile(RouterProfile::from_toml(text)?)
    }

    /// 从配置文件创建路由实例。返回的是 Result<Router, RouterError> 类型。
    pub fn from_profile(profile: RouterProfile) -> Result<Self, RouterError> {
        let algorithm = registry::create_algorithm(&profile.algorithm)?;
        let state = Self::state_from_profile(&profile)?;
        Ok(Self::from_parts(
            algorithm,
            state,
            TargetSet::new(profile.targets.models),
        ))
    }

    /// 按 profile 装配 state 槽。供 PyO3 在注入 Python `StateProvider` 前复用。
    pub fn state_from_profile(
        profile: &RouterProfile,
    ) -> Result<Arc<dyn StateProvider>, RouterError> {
        match profile.state.backend.as_str() {
            "memory" => {
                let ttl: Duration = Duration::from_secs(profile.state.ttl_secs.unwrap_or(300));
                let cap = profile.state.max_entries.unwrap_or(1024);
                Ok(Arc::new(backends::memory::MemoryState::new(ttl, cap)))
            }
            "remote" => {
                let endpoint =
                    profile.state.endpoint.clone().ok_or_else(|| {
                        RouterError::Config("remote state requires endpoint".into())
                    })?;
                let timeout = Duration::from_millis(profile.state.timeout_ms.unwrap_or(5));
                Ok(Arc::new(backends::remote::RemoteState::new(
                    endpoint, timeout,
                )))
            }
            other => Err(RouterError::Config(format!(
                "unknown state backend: {other}"
            ))),
        }
    }

    /// 用已构造的算法与 state 装配。PyO3 可注入 Python 算法或 Python `StateProvider`。
    pub fn from_parts(
        algorithm: Box<dyn AlgorithmProvider>,
        state: Arc<dyn StateProvider>,
        targets: TargetSet,
    ) -> Self {
        Self {
            algorithm,
            state,
            targets,
            seed: AtomicU64::new(0),
            kv_coordinator: None,
        }
    }

    /// 驱动决策循环。`hint` 携带 cache_affinity 等每请求输入。
    /// 返回的是 Result<Decision, RouterError> 类型。
    pub fn route(&self, req: &RouteRequest, hint: &RouteHint) -> Result<Decision, RouterError> {
        let seed = self.seed.fetch_add(1, Ordering::Relaxed);
        // 先于读 state 生成：state 的 `query` 需要它来关联之后带同一 id 回来的
        // 反馈。算法仍然拿不到它——它不进 RouteContext，只在 run 返回后写入 Decision。
        // 路由和反馈是拆开的两步：route 当时只知道请求文本，真正的调用结果（延迟、成败、打分）要等宿主跑完模型才 report。
        // 中间没有别的主键能把这两次调用绑成同一笔决策，所以 runtime 先发一个 id。
        let route_id = Self::next_route_id();
        let mut decision = decide_loop::run(
            // 运行决策循环。
            self.algorithm.as_ref(),
            self.state.as_ref(),
            req,
            hint,
            &self.targets,
            seed,
            &route_id,
        )?;
        decision.route_id = Some(route_id);
        Ok(decision)
    }

    /// 进程内序列保证同进程不同 Router 不重号；不承诺分布式唯一或去重。
    fn next_route_id() -> String {
        static NEXT_ID: AtomicU64 = AtomicU64::new(0);
        let sequence = NEXT_ID.fetch_add(1, Ordering::Relaxed);
        let time = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        format!("{}-{time}-{sequence}", std::process::id())
    }

    /// 校验后转发状态层。
    ///
    /// 校验不过的反馈**不写入 state**，并以 [`FeedbackError`] 返回原因。
    /// 这是跨语言一致的唯一校验入口：Python 门面同样走这里。
    pub fn try_report(&self, feedback: Feedback) -> Result<(), FeedbackError> {
        feedback.validate()?;
        self.state.report(feedback);
        Ok(())
    }

    /// 转发状态层；同步调用，不等待写回完成。
    ///
    /// 兼容入口：内部委托 [`Router::try_report`]，非法反馈**丢弃且不写入 state**。
    /// 需要感知丢弃原因时请改用 `try_report`。
    pub fn report(&self, feedback: Feedback) {
        if self.try_report(feedback).is_err() {
            // 非法反馈按尽力而为语义丢弃：report 无返回值，携带原因需用 try_report。
        }
    }

    pub fn with_kv_coordinator(mut self, cb: Box<dyn KvCacheCoordinator>) -> Self {
        self.set_kv_coordinator(cb);
        self
    }

    pub fn set_kv_coordinator(&mut self, cb: Box<dyn KvCacheCoordinator>) {
        self.kv_coordinator = Some(cb);
    }

    pub fn algorithm_name(&self) -> &str {
        self.algorithm.name()
    }
}

impl RouterProvider for Router {
    fn route(
        &self,
        request: &RouteRequest,
        hint: &RouteHint,
    ) -> Result<ModelSelection, RouterError> {
        Router::route(self, request, hint).map(ModelSelection::from)
    }

    fn try_report(&self, feedback: Feedback) -> Result<(), FeedbackError> {
        Router::try_report(self, feedback)
    }

    fn report(&self, feedback: Feedback) {
        Router::report(self, feedback);
    }

    fn algorithm_name(&self) -> &str {
        Router::algorithm_name(self)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use openjiuwen_protocol::{RequestMetadata, RouteRequest};

    #[test]
    fn passthrough_picks_first_target() {
        let toml = r#"
algorithm = "passthrough"
[state]
backend = "memory"
[targets]
models = ["alpha", "beta"]
"#;
        let router = Router::from_toml(toml).expect("assemble");
        let req = RouteRequest {
            metadata: RequestMetadata {
                session_id: Some("s1".into()),
                agent_id: Some("a1".into()),
            },
            ..RouteRequest::default()
        };
        let d = router.route(&req, &RouteHint::default()).expect("route");
        assert!(d.route_id.as_ref().is_some_and(|id| !id.is_empty()));
        let second = Router::from_toml(toml)
            .unwrap()
            .route(&req, &RouteHint::default())
            .unwrap();
        assert_ne!(d.route_id, second.route_id);
        assert!(Decision::answer("alpha", "algorithm").route_id.is_none());
        assert_eq!(d.selected_model_id, "alpha");
        assert!(d.is_answer_call);

        let plugin: &dyn RouterProvider = &router;
        let selection = plugin
            .route(&req, &RouteHint::default())
            .expect("plugin route");
        assert!(selection.route_id.is_some());
        assert_ne!(selection.route_id, d.route_id);
        assert_eq!(selection.selected_model_id, "alpha");
        assert_eq!(plugin.algorithm_name(), "passthrough");
    }

    /// 非法反馈必须在北向入口被拦下，不能到达 state。
    #[test]
    fn invalid_feedback_does_not_reach_state() {
        use openjiuwen_protocol::{Extension, Feedback, RoutingKey, Value};

        let router = Router::from_toml(
            r#"
algorithm = "passthrough"
[state]
backend = "memory"
[targets]
models = ["alpha"]
"#,
        )
        .expect("assemble");
        let key = RoutingKey {
            session_id: "s1".into(),
            agent_id: "a1".into(),
        };

        let mut bad_version = Feedback::ok(key.clone(), "alpha", 1);
        bad_version.version = 999;
        assert_eq!(
            router.try_report(bad_version),
            Err(openjiuwen_protocol::FeedbackError::UnsupportedVersion)
        );

        let mut bad_payload = Feedback::ok(key.clone(), "alpha", 1);
        bad_payload.extensions = vec![Extension {
            schema: "s".into(),
            version: "1".into(),
            data: Value::Float(f64::NAN),
        }];
        assert_eq!(
            router.try_report(bad_payload),
            Err(openjiuwen_protocol::FeedbackError::NonFiniteNumber)
        );

        // 以上非法反馈均被拦下，state 未被更新。
        assert_eq!(router.state.snapshot(&key).stats.sample_count, 0);

        // 旧入口同样不写入非法数据。
        let mut legacy_bad = Feedback::ok(key.clone(), "alpha", 1);
        legacy_bad.version = 999;
        router.report(legacy_bad);
        assert_eq!(router.state.snapshot(&key).stats.sample_count, 0);

        // 合法反馈正常写入。
        assert_eq!(
            router.try_report(Feedback::ok(key.clone(), "alpha", 1)),
            Ok(())
        );
        assert_eq!(router.state.snapshot(&key).stats.sample_count, 1);
    }

    /// state 的 `query` 收到的 `route_id` 必须与 `route` 返回的一致，且覆盖宿主自填的值；
    /// 没有检索意图时不调 `query`，id 照常生成。
    #[test]
    fn query_receives_the_route_id_the_decision_carries() {
        use openjiuwen_protocol::{RoutingKey, StateQuery, StateQueryError, StateSnapshot, StateView};
        use std::sync::Mutex;

        struct Recorder(Mutex<Vec<Option<String>>>);
        impl StateProvider for Recorder {
            fn snapshot(&self, _key: &RoutingKey) -> StateView {
                StateView::empty()
            }
            fn query(&self, _key: &RoutingKey, query: &StateQuery) -> Result<StateSnapshot, StateQueryError> {
                self.0.lock().unwrap().push(query.route_id.clone());
                Ok(StateSnapshot::empty())
            }
            fn report(&self, _feedback: Feedback) {}
        }

        let recorder = Arc::new(Recorder(Mutex::new(Vec::new())));
        let router = Router::from_parts(
            registry::create_algorithm("passthrough").expect("passthrough"),
            recorder.clone(),
            TargetSet::new(["alpha"]),
        );
        let req = RouteRequest::default();

        let hint = RouteHint {
            state_query: Some(StateQuery::text("hello").with_route_id("host-filled")),
            ..RouteHint::default()
        };
        let decision = router.route(&req, &hint).expect("route");
        assert_eq!(recorder.0.lock().unwrap().as_slice(), &[decision.route_id.clone()]);
        assert_ne!(decision.route_id.as_deref(), Some("host-filled"));

        let plain = router.route(&req, &RouteHint::default()).expect("route");
        assert!(plain.route_id.is_some());
        assert_ne!(plain.route_id, decision.route_id);
        assert_eq!(recorder.0.lock().unwrap().len(), 1, "no state_query → query not called");
    }

    #[test]
    fn stage_router_assembles_from_profile() {
        let toml = r#"
algorithm = "stage_router"
[state]
backend = "memory"
[targets]
models = ["efficient", "capable"]
"#;
        let router = Router::from_toml(toml).expect("assemble stage router");
        let decision = router
            .route(&RouteRequest::default(), &RouteHint::default())
            .expect("route");

        assert_eq!(router.algorithm_name(), "stage_router");
        assert_eq!(decision.selected_model_id, "efficient");
        assert!(decision.reasoning.contains("source=fall_open"));
    }
}
