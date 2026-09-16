//! L3 算法层。公共插件契约与测试实现分离。

pub mod algorithm_provider;
pub mod evolving_provider;
#[cfg(feature = "algo-stage_router")]
pub mod stage_router;
pub mod test_algo;

pub use algorithm_provider::{AlgorithmProvider, RouteContext};
pub use evolving_provider::{Artifact, EvolvingProvider, TrainingBatch};
pub use openjiuwen_protocol::TrainingPrompt;

/// 旧版算法模块路径的兼容导出。新代码应从 crate 根导入契约。
pub mod algorithm {
    pub use crate::algorithm_provider::{AlgorithmProvider, RouteContext};
    pub use crate::test_algo::evolving;
    pub use crate::test_algo::routing as test_algorithm;
}

/// 旧版自演进模块路径的兼容导出。新代码应从 crate 根导入契约。
pub mod evolving {
    #[doc(hidden)]
    pub use crate::evolving_provider as evolving_provider;
    pub use crate::evolving_provider::{Artifact, EvolvingProvider, TrainingBatch};
    pub use openjiuwen_protocol::TrainingPrompt;

    pub use crate::test_algo::evolving as test_evolving;
}
