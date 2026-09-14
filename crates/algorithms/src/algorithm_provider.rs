//! [`AlgorithmProvider`]：算法实现者的唯一接入点。
//!
//! 纯函数，不持有可变状态。同样的 `(request, ctx)` 必须返回同样的 [`Decision`]。
//!
//! 三条边界：
//!
//! 1. **不得调用被选中的目标模型。** 决策止于返回 [`Decision`] 里的
//!    `selected_model_id`；调用目标模型由宿主（runtime / host）负责。
//! 2. **可以自带决策辅助模型**（如复杂度分类器），它服务的是决策本身，
//!    与第 1 条不冲突。
//! 3. **调用应尽量保持无状态纯调用**：无论自带模型还是外部（含云端）
//!    辅助模型，都应避免在调用链中引入可变状态（缓存、会话粘性等），
//!    以免削弱可重放性。这是**实现者自身的责任**，框架不强制约束。

use openjiuwen_protocol::{
    Decision, RetrievedItem, RouteRequest, RouterError, StateView, TargetSet,
};

/// 注入给算法的只读上下文。状态是 hint，`view` 可为空。
#[derive(Clone, Debug)]
pub struct RouteContext {
    /// 本次可选目标（已剔除 exclusions）。
    pub targets: TargetSet,
    /// 状态快照（可为空，算法必须能降级）。
    pub view: StateView,
    /// 状态检索命中（可为空）。宿主未发起检索、或 state 未实现 `query` 时为空。
    ///
    /// 按 `score` 降序，长度受 `QUERY_MAX_RETRIEVED` 约束。算法必须能在空
    /// 命中下降级为冷路由。
    pub retrieved: Vec<RetrievedItem>,
    /// 随机性显式注入，保证可重放。
    pub seed: u64,
}

/// 算法槽插件契约。与 state 侧 `StateProvider` 对位：运行期单槽选一。
pub trait AlgorithmProvider: Send + Sync {
    /// 稳定的低基数名称，用于注册表与遥测。
    fn name(&self) -> &str;

    /// 单步决策。读请求与状态快照，直接返回选中的目标。
    fn decide(&self, request: &RouteRequest, ctx: &RouteContext) -> Result<Decision, RouterError>;
}
