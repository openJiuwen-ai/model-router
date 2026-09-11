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

    /// 带检索参数的快照。`snapshot` 的平级可选扩展。
    ///
    /// 默认实现**忽略检索参数**并降级为 [`StateProvider::snapshot`]，因此
    /// 既有的 state 实现无需任何改动即可继续工作；需要向量检索等能力的实现
    /// 覆盖本方法即可。
    ///
    /// 与 `snapshot` 的纪律一致：实现必须自行降级，**不得**因检索失败而阻断
    /// 请求——返回仅含视图的结果，或返回 `Err` 交由 runtime 回退。runtime 对
    /// `Err` 的处理是回退到 `snapshot`，不影响路由可用性。
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
