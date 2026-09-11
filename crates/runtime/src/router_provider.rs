//! [`RouterProvider`]：应用嵌入本路由内核时的北向契约。
//!
//! 与 [`Router`](crate::Router) 同层：装配（`from_config`）在 `Router` 上，本 trait 只规定运行期怎么问、怎么报。
//! `route` 返回跨边界规格 [`ModelSelection`]，不是内部的 [`Decision`](openjiuwen_protocol::Decision)。

use openjiuwen_protocol::{
    Feedback, FeedbackError, ModelSelection, RouteHint, RouteRequest, RouterError,
};

/// 北向门面契约。`Send + Sync`，对象安全，应用可持有 `&dyn RouterProvider`。
pub trait RouterProvider: Send + Sync {
    /// 单步决策。`hint` 无额外信息时传 [`RouteHint::default`]。
    fn route(
        &self,
        request: &RouteRequest,
        hint: &RouteHint,
    ) -> Result<ModelSelection, RouterError>;

    /// 校验后吸收调用结果；非法反馈不写入 state。
    ///
    /// 同步调用，不等待写回完成。实现方必须先校验再转发。
    fn try_report(&self, feedback: Feedback) -> Result<(), FeedbackError>;

    /// 吸收调用结果。同步调用，不等待写回完成。
    ///
    /// 兼容入口：非法反馈丢弃且不写入 state；需要感知原因用 [`RouterProvider::try_report`]。
    fn report(&self, feedback: Feedback);

    /// 当前算法槽的稳定名，供日志与遥测。
    fn algorithm_name(&self) -> &str;
}
