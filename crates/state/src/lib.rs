//! L2 状态管理层。公共插件契约与测试实现分离。
//!
//! runtime 只面向 [`StateProvider`]；内存或远程由 profile 决定。

pub mod state_provider;
pub mod test_state;

#[cfg(feature = "service")]
pub mod service;

pub use state_provider::{CasConflict, StateProvider};
pub use test_state::{MemoryState, RemoteState};

/// 旧版状态模块路径的兼容导出。新代码应从 crate 根导入契约和实现。
pub mod state {
    pub use crate::state_provider;
    pub use crate::state_provider::{CasConflict, StateProvider};
    pub use crate::test_state;
}
