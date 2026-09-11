//! [`AlgorithmProvider`]：算法实现者的唯一接入点。
//!
//! 纯函数，不做 I/O、不持有可变状态。同样的 `(request, ctx)` 必须返回同样的 [`Decision`]。

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
