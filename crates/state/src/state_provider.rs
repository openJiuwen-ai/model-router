//! [`StateProvider`]：状态实现者的唯一接入点。
//!
//! 与算法侧 `AlgorithmProvider` 对位：运行期单槽选一。
//! 状态是 hint：有界、可丢失；算法从不直接调用本 trait。

use openjiuwen_protocol::{
    Feedback, RoutingKey, StateQuery, StateQueryError, StateSnapshot, StateView,
};

/// CAS 写回冲突：期望版本与当前 active 不一致。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CasConflict {
    pub slot: String,
    pub expected: u64,
    pub actual: u64,
}

/// 跨请求记忆的插件契约。状态是 hint：有界、可丢失。
///
/// 远程实现超时必须返回空 [`StateView`] 而非 `Err`，绝不阻塞请求。
pub trait StateProvider: Send + Sync {
    /// 路由前一次性快照。基础读取路径，入参只有路由键。
    fn snapshot(&self, key: &RoutingKey) -> StateView;

    /// 按 [`StateQuery`] 做一次检索读。与 [`StateProvider::snapshot`] 平级，不是它的带参变体。
    ///
    /// 返回 [`StateSnapshot`]：`view` 仍是按键的 hint；`retrieved` 是命中列表，
    /// `StateQuery::top_k` 只表示条数上限（实际可更少，空列表合法）。
    /// `StateQuery::route_id` 由 runtime 注入，实现可用它把本次检索与之后的
    /// [`StateProvider::report`] 对上。
    ///
    /// 默认实现忽略检索入参，只包一层 [`StateProvider::snapshot`]，既有实现
    /// 不覆盖也能工作；需要文本 / 向量检索时再覆盖。
    ///
    /// 检索失败不得阻断请求：返回仅含视图的结果，或返回 `Err` 让 runtime
    /// 回退到 `snapshot`。
    fn query(&self, key: &RoutingKey, query: &StateQuery) -> Result<StateSnapshot, StateQueryError> {
        let _ = query;
        Ok(StateSnapshot::from_view(self.snapshot(key)))
    }

    /// 吸收路由后反馈。异步、尽力而为；本骨架同步写入。
    fn report(&self, feedback: Feedback);

    /// 带版本原子写回（训练任务 → state）。默认实现为 no-op。
    fn publish(&self, slot: &str, artifact: &[u8], ver: u64) -> Result<(), CasConflict> {
        let _ = (slot, artifact, ver);
        Ok(())
    }
}
