//! 状态检索入参。`snapshot` 的平级可选扩展。
//!
//! 宿主在路由时显式给出检索意图；state 实现按需覆盖 `StateProvider::query` 即可。
//! 未覆盖的实现由 trait 默认实现降级为 `snapshot`，行为与旧版完全一致。
//!
//! 状态是 hint：所有上限都是硬约束，超限一律拒绝，绝不无限增长。

use crate::feedback::{Extension, FeedbackError, Value, MAX_EXTENSIONS};
use crate::state_view::StateView;

/// 查询文本最大 UTF-8 字节数。
pub const QUERY_MAX_TEXT_BYTES: usize = 16_384;
/// 查询向量最大维度。
pub const QUERY_MAX_VECTOR_DIMS: usize = 4_096;
/// 算法可请求的 `top_k` 上限。
pub const QUERY_MAX_TOP_K: u32 = 256;
/// 单次检索结果最大条数。
pub const QUERY_MAX_RETRIEVED: usize = 256;

/// 检索入参。字段全部可选，`None` 表示不约束该维度。
///
/// * `text`：查询文本；embedding 由 state 实现自行完成（那属于 state 的 I/O）。
/// * `vector`：宿主预计算的查询向量；与 `text` 二选一或同时给出。
/// * `top_k`：top_k 是这次检索希望最多拿回多少条 RetrievedItem
/// * `extensions`：厂商私有检索参数，复用 [`Extension`] 版本化载荷。
/// * `route_id`：一次 `route` 的关联 id，**由 runtime 在调用 `query` 前填入**；宿主构造时
///   留空，填了也会被覆盖。与随后 `Decision.route_id` / `Feedback.route_id`
///   是同一个值，让 state 能把检索时看到的上下文与之后到达的反馈对上——例如在
///   `query` 时为该次路由开一条待回填的记录，等 `report` 带同一 id 回来时关闭。
///   `is_empty` / `validate` 不看这个字段。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct StateQuery {
    pub text: Option<String>,
    pub vector: Option<Vec<f32>>,
    pub top_k: Option<u32>,
    pub extensions: Vec<Extension>,
    pub route_id: Option<String>,
}

impl StateQuery {
    /// 文本检索查询。
    pub fn text(text: impl Into<String>) -> Self {
        Self {
            text: Some(text.into()),
            ..Self::default()
        }
    }

    /// 向量检索查询。
    pub fn vector(vector: impl Into<Vec<f32>>) -> Self {
        Self {
            vector: Some(vector.into()),
            ..Self::default()
        }
    }

    /// 设置期望返回条数。
    pub fn with_top_k(mut self, top_k: u32) -> Self {
        self.top_k = Some(top_k);
        self
    }

    /// 由 runtime 调用：标记本次查询所属的 `route`。
    pub fn with_route_id(mut self, route_id: impl Into<String>) -> Self {
        self.route_id = Some(route_id.into());
        self
    }

    /// 是否未携带任何检索信息。此时调用方应直接走 `snapshot`。
    pub fn is_empty(&self) -> bool {
        self.text.is_none()
            && self.vector.is_none()
            && self.top_k.is_none()
            && self.extensions.is_empty()
    }

    /// 严格校验：文本长度、向量维度与有限性、`top_k` 范围、扩展载荷预算。
    pub fn validate(&self) -> Result<(), StateQueryError> {
        if let Some(text) = &self.text {
            if text.len() > QUERY_MAX_TEXT_BYTES {
                return Err(StateQueryError::TextTooLarge);
            }
        }
        if let Some(vector) = &self.vector {
            if vector.is_empty() {
                return Err(StateQueryError::EmptyVector);
            }
            if vector.len() > QUERY_MAX_VECTOR_DIMS {
                return Err(StateQueryError::VectorTooLarge);
            }
            if vector.iter().any(|v| !v.is_finite()) {
                return Err(StateQueryError::NonFiniteVector);
            }
        }
        if let Some(top_k) = self.top_k {
            if top_k == 0 || top_k > QUERY_MAX_TOP_K {
                return Err(StateQueryError::InvalidTopK);
            }
        }
        if self.extensions.len() > MAX_EXTENSIONS {
            return Err(StateQueryError::Extension(FeedbackError::PayloadTooLarge));
        }
        let mut count = 0;
        let mut bytes = 0;
        for ext in &self.extensions {
            ext.validate().map_err(StateQueryError::Extension)?;
            bytes += ext.schema.len() + ext.version.len();
            ext.data
                .validate_inner(1, &mut count, &mut bytes)
                .map_err(StateQueryError::Extension)?;
        }
        Ok(())
    }
}

/// 单条检索结果。
///
/// * `id`：宿主可解释的条目标识。
/// * `score`：相似度或距离；语义由 state 实现与其 schema 约定。
/// * `data`：可选载荷，受 [`Value`] 同一套预算约束。
#[derive(Clone, Debug, PartialEq)]
pub struct RetrievedItem {
    pub id: String,
    pub score: f64,
    pub data: Option<Value>,
}

impl RetrievedItem {
    /// 仅含标识与分数的结果项。
    pub fn new(id: impl Into<String>, score: f64) -> Self {
        Self {
            id: id.into(),
            score,
            data: None,
        }
    }
}

/// 一次状态读取的完整结果：稳定性字段 + 可选检索命中。
///
/// `view` 与 `StateProvider::snapshot` 的返回同构；
/// `retrieved` 为空即等价于旧版行为。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct StateSnapshot {
    pub view: StateView,
    pub retrieved: Vec<RetrievedItem>,
}

impl StateSnapshot {
    /// 空结果：算法必须能在空视图与空命中下降级为冷路由。
    pub fn empty() -> Self {
        Self::default()
    }

    /// 仅带视图、无检索命中的结果。
    pub fn from_view(view: StateView) -> Self {
        Self {
            view,
            retrieved: Vec::new(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.view.is_empty() && self.retrieved.is_empty()
    }

    /// 防御性校验：条数上限、分数有限、载荷预算。
    ///
    /// runtime 会在交给算法前调用；不合规的快照降级为仅视图，绝不阻断请求。
    pub fn validate(&self) -> Result<(), StateQueryError> {
        if self.retrieved.len() > QUERY_MAX_RETRIEVED {
            return Err(StateQueryError::TooManyRetrieved);
        }
        let mut count = 0;
        let mut bytes = 0;
        for item in &self.retrieved {
            if !item.score.is_finite() {
                return Err(StateQueryError::NonFiniteScore);
            }
            if let Some(data) = &item.data {
                data.validate_inner(1, &mut count, &mut bytes)
                    .map_err(StateQueryError::Extension)?;
            }
        }
        Ok(())
    }
}

/// 检索入参 / 结果校验错误。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StateQueryError {
    /// 向量维度为 0。
    EmptyVector,
    /// 向量维度超过 [`QUERY_MAX_VECTOR_DIMS`]。
    VectorTooLarge,
    /// 向量含 NaN / Infinity。
    NonFiniteVector,
    /// 查询文本超过 [`QUERY_MAX_TEXT_BYTES`]。
    TextTooLarge,
    /// `top_k` 为 0 或超过 [`QUERY_MAX_TOP_K`]。
    InvalidTopK,
    /// 返回条数超过 [`QUERY_MAX_RETRIEVED`]。
    TooManyRetrieved,
    /// 结果分数为 NaN / Infinity。
    NonFiniteScore,
    /// 实现侧自身失败（如跨语言桥接调用异常）。runtime 会回退到 `snapshot`。
    Backend(String),
    /// 扩展载荷不合规，复用反馈侧的同一套错误。
    Extension(FeedbackError),
}

impl std::fmt::Display for StateQueryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::EmptyVector => write!(f, "query vector must not be empty"),
            Self::VectorTooLarge => {
                write!(f, "query vector exceeds {QUERY_MAX_VECTOR_DIMS} dimensions")
            }
            Self::NonFiniteVector => write!(f, "query vector contains a non-finite number"),
            Self::TextTooLarge => {
                write!(f, "query text exceeds {QUERY_MAX_TEXT_BYTES} bytes")
            }
            Self::InvalidTopK => {
                write!(f, "top_k must be in 1..={QUERY_MAX_TOP_K}")
            }
            Self::TooManyRetrieved => {
                write!(f, "retrieved items exceed {QUERY_MAX_RETRIEVED}")
            }
            Self::NonFiniteScore => write!(f, "retrieved item score is not finite"),
            Self::Backend(msg) => write!(f, "state query backend failed: {msg}"),
            Self::Extension(err) => write!(f, "extension: {err}"),
        }
    }
}

impl std::error::Error for StateQueryError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_query_is_detected() {
        assert!(StateQuery::default().is_empty());
        assert!(!StateQuery::text("hi").is_empty());
        assert!(!StateQuery::vector(vec![0.0, 1.0]).is_empty());
        assert!(!StateQuery::default().with_top_k(3).is_empty());
    }

    #[test]
    fn text_and_vector_bounds() {
        assert!(StateQuery::text("x".repeat(QUERY_MAX_TEXT_BYTES)).validate().is_ok());
        assert_eq!(
            StateQuery::text("x".repeat(QUERY_MAX_TEXT_BYTES + 1)).validate(),
            Err(StateQueryError::TextTooLarge)
        );
        assert_eq!(
            StateQuery::vector(Vec::new()).validate(),
            Err(StateQueryError::EmptyVector)
        );
        assert_eq!(
            StateQuery::vector(vec![0.0f32; QUERY_MAX_VECTOR_DIMS + 1]).validate(),
            Err(StateQueryError::VectorTooLarge)
        );
        assert_eq!(
            StateQuery::vector(vec![f32::NAN]).validate(),
            Err(StateQueryError::NonFiniteVector)
        );
        assert_eq!(
            StateQuery::text("q").with_top_k(0).validate(),
            Err(StateQueryError::InvalidTopK)
        );
        assert_eq!(
            StateQuery::text("q").with_top_k(QUERY_MAX_TOP_K + 1).validate(),
            Err(StateQueryError::InvalidTopK)
        );
        assert!(StateQuery::vector(vec![0.0f32; QUERY_MAX_VECTOR_DIMS])
            .with_top_k(QUERY_MAX_TOP_K)
            .validate()
            .is_ok());
    }

    #[test]
    fn snapshot_rejects_unbounded_or_non_finite() {
        let mut snapshot = StateSnapshot::from_view(StateView::empty());
        snapshot.retrieved = vec![RetrievedItem::new("a", f64::INFINITY)];
        assert_eq!(snapshot.validate(), Err(StateQueryError::NonFiniteScore));

        let mut snapshot = StateSnapshot::empty();
        snapshot.retrieved = (0..=QUERY_MAX_RETRIEVED)
            .map(|i| RetrievedItem::new(format!("i{i}"), 0.0))
            .collect();
        assert_eq!(snapshot.validate(), Err(StateQueryError::TooManyRetrieved));

        assert!(StateSnapshot::empty().validate().is_ok());
    }

    #[test]
    fn extension_budget_is_shared_across_entries() {
        let ext = Extension {
            schema: "vendor.knn".into(),
            version: "1".into(),
            data: Value::String("x".repeat(40_000)),
        };
        let query = StateQuery {
            text: None,
            vector: None,
            top_k: None,
            extensions: vec![ext; 2],
            ..StateQuery::default()
        };
        assert_eq!(
            query.validate(),
            Err(StateQueryError::Extension(FeedbackError::PayloadTooLarge))
        );
    }

    /// `route_id` 是 runtime 的关联字段，不参与「是否为空」与校验。
    #[test]
    fn route_id_is_transparent_to_emptiness_and_validation() {
        let query = StateQuery::default().with_route_id("d-1");
        assert!(query.is_empty());
        assert!(query.validate().is_ok());
        assert_eq!(query.route_id.as_deref(), Some("d-1"));
    }
}
