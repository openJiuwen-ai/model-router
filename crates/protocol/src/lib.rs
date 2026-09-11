//! L1 协议层：全部跨模块类型。零依赖底座——只有数据、没有行为。
//!
//! 其他 crate 只经本层对话；任何一层可独立替换。

pub mod decision;
pub mod error;
pub mod feedback;
pub mod request;
pub mod selection;
pub mod state_query;
pub mod state_view;

pub use decision::Decision;
pub use error::RouterError;
pub use feedback::{
    CallFeedback, Extension, Feedback, FeedbackError, Outcome, Value, MAX_EXTENSIONS,
    VALUE_MAX_BYTES,
};
pub use request::{Message, RequestMetadata, RouteHint, RouteRequest, RoutingKey, TargetSet};
pub use selection::ModelSelection;
pub use state_query::{
    RetrievedItem, StateQuery, StateQueryError, StateSnapshot, QUERY_MAX_RETRIEVED,
    QUERY_MAX_TEXT_BYTES, QUERY_MAX_TOP_K, QUERY_MAX_VECTOR_DIMS,
};
pub use state_view::{FeedbackStats, StateView};
