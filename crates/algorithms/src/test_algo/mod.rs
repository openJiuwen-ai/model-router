//! 测试实现集合；公共插件契约位于 crate 根模块。

pub mod evolving;
pub mod routing;

// 兼容旧版公开路径；契约定义仍位于 crate 根模块。
#[doc(hidden)]
pub use crate::algorithm_provider;
#[doc(hidden)]
pub use crate::algorithm_provider::{AlgorithmProvider, RouteContext};
#[doc(hidden)]
pub use routing as test_algorithm;
