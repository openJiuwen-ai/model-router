//! L4 装配与运行。宿主只看 [`Router`]；北向契约是 [`RouterProvider`]。

pub mod config;
pub mod decide_loop;
pub mod registry;
pub mod router;
pub mod router_provider;
pub mod training;
pub mod trigger;

pub use config::RouterProfile;
pub use openjiuwen_protocol::{
    Decision, Feedback, Message, ModelSelection, Outcome, RequestMetadata, RouteHint, RouteRequest,
    RouterError, RoutingKey, ToolCall,
};
pub use registry::create_algorithm;
pub use router::{KvCacheCoordinator, Router};
pub use router_provider::RouterProvider;
