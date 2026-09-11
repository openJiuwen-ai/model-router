//! 宿主回报的一次调用结果。语义失败不在此列。

use crate::RoutingKey;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Ok,
    Overflow,
    Unavailable,
    Rejected,
}

/// 载荷预算：UTF-8 字符串、键、schema/version 的字节数加其他节点每个 8 字节；非 JSON 编码长度。
pub const VALUE_MAX_BYTES: usize = 65_536;
pub const MAX_EXTENSIONS: usize = 32;

/// 协议版本号；用于新旧字段兼容。
pub const FEEDBACK_VERSION: u32 = 1;

/// 扩展载荷嵌套深度上限（含根）。
pub const VALUE_MAX_DEPTH: usize = 8;
/// 扩展载荷元素总数上限（含根）。
pub const VALUE_MAX_ELEMENTS: usize = 256;

/// 反馈校验错误。扩展严格校验，便于 Rust / Python 统一报错。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FeedbackError {
    EmptySchema,
    EmptyVersion,
    NestedTooDeep,
    PayloadTooLarge,
    NonFiniteNumber,
    IntegerOverflow,
    DuplicateKey,
    UnsupportedVersion,
}

impl std::fmt::Display for FeedbackError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DuplicateKey => write!(f, "extension object contains duplicate keys"),
            Self::UnsupportedVersion => write!(f, "unsupported feedback version"),
            Self::EmptySchema => write!(f, "extension schema must be non-empty"),
            Self::EmptyVersion => write!(f, "extension version must be non-empty"),
            Self::NestedTooDeep => {
                write!(
                    f,
                    "extension data exceeds max nesting depth {VALUE_MAX_DEPTH}"
                )
            }
            Self::PayloadTooLarge => {
                write!(
                    f,
                    "extensions exceed size limits (nodes={VALUE_MAX_ELEMENTS}, bytes={VALUE_MAX_BYTES}, extensions={MAX_EXTENSIONS})"
                )
            }
            Self::NonFiniteNumber => write!(f, "extension data contains a non-finite number"),
            Self::IntegerOverflow => {
                write!(f, "extension data contains an integer out of i64 range")
            }
        }
    }
}

impl std::error::Error for FeedbackError {}

/// 受约束的跨语言 JSON 值。协议层零依赖，故自定义实现：
/// 严格禁止非有限数、整数越界、过度嵌套与过大载荷。
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Integer(i64),
    Float(f64),
    String(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

impl Value {
    /// 严格校验：嵌套深度、元素总数、非有限数。
    pub fn validate(&self) -> Result<(), FeedbackError> {
        let mut count = 0usize;
        self.validate_inner(1, &mut count, &mut 0)
    }

    fn validate_inner(
        &self,
        depth: usize,
        count: &mut usize,
        bytes: &mut usize,
    ) -> Result<(), FeedbackError> {
        *bytes += match self {
            Value::String(s) => s.len(),
            _ => 8,
        };
        if *bytes > VALUE_MAX_BYTES {
            return Err(FeedbackError::PayloadTooLarge);
        }
        if depth > VALUE_MAX_DEPTH {
            return Err(FeedbackError::NestedTooDeep);
        }
        *count += 1;
        if *count > VALUE_MAX_ELEMENTS {
            return Err(FeedbackError::PayloadTooLarge);
        }
        match self {
            Value::Float(f) if !f.is_finite() => Err(FeedbackError::NonFiniteNumber),
            Value::Array(items) => {
                for it in items {
                    it.validate_inner(depth + 1, count, bytes)?;
                }
                Ok(())
            }
            Value::Object(pairs) => {
                let mut keys = std::collections::HashSet::new();
                for (k, v) in pairs {
                    if !keys.insert(k) {
                        return Err(FeedbackError::DuplicateKey);
                    }
                    *bytes += k.len();
                    v.validate_inner(depth + 1, count, bytes)?;
                }
                Ok(())
            }
            _ => Ok(()),
        }
    }
}

/// 单次调用的结果。`latency_ms` / `cache_valid` 可缺省（延迟评价或未知）。
#[derive(Clone, Debug, PartialEq)]
pub struct CallFeedback {
    pub outcome: Outcome,
    pub latency_ms: Option<u64>,
    pub cache_valid: Option<bool>,
}

/// 宿主侧任意扩展载荷。`schema` 非空版本，`data` 为受约束 JSON 值。
///
/// 本最小版本不做 schema 注册表：未知 schema 也按值透传并严格校验（即明确不承诺语义）。
#[derive(Clone, Debug, PartialEq)]
pub struct Extension {
    pub schema: String,
    pub version: String,
    pub data: Value,
}

impl Extension {
    /// 严格校验：schema / version 非空，data 受约束。
    pub fn validate(&self) -> Result<(), FeedbackError> {
        if self.schema.trim().is_empty() {
            return Err(FeedbackError::EmptySchema);
        }
        if self.version.trim().is_empty() {
            return Err(FeedbackError::EmptyVersion);
        }
        let mut bytes = self.schema.len() + self.version.len();
        self.data.validate_inner(1, &mut 0, &mut bytes)
    }
}

/// 反馈入参（具体非泛型结构）。
///
/// * `version`：协议版本。
/// * `event_id`：宿主关联事件 id；缺失表示未知（旧数据 / 未关联）。
/// * `decision_id`：runtime 生成的唯一决策 id；缺失表示未知。
/// * `key`：路由键。
/// * `selected_model_id`：选中目标模型 id。
/// * `observed_at_ms`：观测时刻；缺失表示未知。
/// * `call`：本次调用结果；`None` 表示延迟 / 未知评价。
/// * `extensions`：宿主扩展载荷。
#[derive(Clone, Debug, PartialEq)]
pub struct Feedback {
    pub version: u32,
    pub event_id: Option<String>,
    pub decision_id: Option<String>,
    pub key: RoutingKey,
    pub selected_model_id: String,
    pub observed_at_ms: Option<u64>,
    pub call: Option<CallFeedback>,
    pub extensions: Vec<Extension>,
}

impl Feedback {
    /// 创建一个成功反馈（兼容旧 `Feedback::ok`）。
    ///
    /// 旧数据允许缺失关联：`event_id` / `decision_id` / `observed_at_ms` 设为 `None` 表示未知。
    pub fn ok(key: RoutingKey, selected_model_id: impl Into<String>, latency_ms: u64) -> Self {
        Self {
            version: FEEDBACK_VERSION,
            event_id: None,
            decision_id: None,
            key,
            selected_model_id: selected_model_id.into(),
            observed_at_ms: None,
            call: Some(CallFeedback {
                outcome: Outcome::Ok,
                latency_ms: Some(latency_ms),
                cache_valid: None,
            }),
            extensions: Vec::new(),
        }
    }

    /// 创建一条延迟反馈：`call` 为 `None`，表示调用结果尚未可知。
    pub fn delayed(key: RoutingKey, selected_model_id: impl Into<String>) -> Self {
        Self {
            version: FEEDBACK_VERSION,
            event_id: None,
            decision_id: None,
            key,
            selected_model_id: selected_model_id.into(),
            observed_at_ms: None,
            call: None,
            extensions: Vec::new(),
        }
    }

    /// 校验协议版本、全部扩展及它们的合计节点/字节预算。
    pub fn validate(&self) -> Result<(), FeedbackError> {
        if self.version != FEEDBACK_VERSION {
            return Err(FeedbackError::UnsupportedVersion);
        }
        if self.extensions.len() > MAX_EXTENSIONS {
            return Err(FeedbackError::PayloadTooLarge);
        }
        let mut count = 0;
        let mut bytes = 0;
        for ext in &self.extensions {
            ext.validate()?;
            bytes += ext.schema.len() + ext.version.len();
            ext.data.validate_inner(1, &mut count, &mut bytes)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> RoutingKey {
        RoutingKey {
            session_id: "s".into(),
            agent_id: "a".into(),
        }
    }

    #[test]
    fn limits_and_duplicate_keys() {
        assert!(Value::String("x".repeat(VALUE_MAX_BYTES))
            .validate()
            .is_ok());
        assert!(Value::String("x".repeat(VALUE_MAX_BYTES + 1))
            .validate()
            .is_err());
        let duplicate = Value::Object(vec![("k".into(), Value::Null), ("k".into(), Value::Null)]);
        assert_eq!(duplicate.validate(), Err(FeedbackError::DuplicateKey));
        let mut value = Value::Null;
        for _ in 1..VALUE_MAX_DEPTH {
            value = Value::Array(vec![value]);
        }
        assert!(value.validate().is_ok());
        assert_eq!(
            Value::Array(vec![value]).validate(),
            Err(FeedbackError::NestedTooDeep)
        );
        let mut fb = Feedback::delayed(key(), "m");
        fb.extensions = vec![
            Extension {
                schema: "s".into(),
                version: "1".into(),
                data: Value::String("x".repeat(40_000))
            };
            2
        ];
        assert_eq!(fb.validate(), Err(FeedbackError::PayloadTooLarge));
    }

    #[test]
    fn ok_shape_has_call() {
        let fb = Feedback::ok(key(), "m", 12);
        assert_eq!(fb.version, FEEDBACK_VERSION);
        assert!(fb.event_id.is_none());
        assert!(fb.decision_id.is_none());
        let c = fb.call.expect("ok has call");
        assert_eq!(c.outcome, Outcome::Ok);
        assert_eq!(c.latency_ms, Some(12));
        assert!(fb.extensions.is_empty());
    }

    #[test]
    fn delayed_has_no_call() {
        let fb = Feedback::delayed(key(), "m");
        assert!(fb.call.is_none());
        assert!(fb.validate().is_ok());
    }

    #[test]
    fn value_rejects_non_finite() {
        assert_eq!(
            Value::Float(f64::INFINITY).validate(),
            Err(FeedbackError::NonFiniteNumber)
        );
        assert_eq!(
            Value::Float(f64::NAN).validate(),
            Err(FeedbackError::NonFiniteNumber)
        );
        assert!(Value::Float(1.5).validate().is_ok());
    }

    #[test]
    fn value_rejects_too_deep() {
        let mut deep = Value::Array(vec![]);
        for _ in 0..(VALUE_MAX_DEPTH + 2) {
            deep = Value::Array(vec![deep]);
        }
        assert_eq!(deep.validate(), Err(FeedbackError::NestedTooDeep));
    }

    #[test]
    fn value_rejects_too_large() {
        let mut arr = Vec::new();
        for _ in 0..(VALUE_MAX_ELEMENTS + 2) {
            arr.push(Value::Bool(true));
        }
        assert_eq!(
            Value::Array(arr).validate(),
            Err(FeedbackError::PayloadTooLarge)
        );
    }

    #[test]
    fn extension_schema_and_version_required() {
        let ext = Extension {
            schema: "".into(),
            version: "1".into(),
            data: Value::Null,
        };
        assert_eq!(ext.validate(), Err(FeedbackError::EmptySchema));
        let ext = Extension {
            schema: "s".into(),
            version: "".into(),
            data: Value::Null,
        };
        assert_eq!(ext.validate(), Err(FeedbackError::EmptyVersion));
    }

    #[test]
    fn unknown_schema_passes_validation() {
        // 最小版本不做 schema 注册表：未知 schema 仅做结构校验。
        let ext = Extension {
            schema: "vendor.unknown.v1".into(),
            version: "0.0.1".into(),
            data: Value::Object(vec![("k".into(), Value::Integer(1))]),
        };
        assert!(ext.validate().is_ok());
    }

    #[test]
    fn feedback_validate_with_extension() {
        let mut fb = Feedback::ok(key(), "m", 1);
        fb.extensions.push(Extension {
            schema: "".into(),
            version: "1".into(),
            data: Value::Null,
        });
        assert_eq!(fb.validate(), Err(FeedbackError::EmptySchema));
    }

    /// 预算按“节点开销相加”计算，组合载荷在临界值上必须与逐节点计费一致。
    /// 若实现采用“先统一 +8 再对字符串回退”，这里会误判超限。
    #[test]
    fn byte_budget_counts_combination_at_boundary() {
        let max = VALUE_MAX_BYTES;
        // 容器 8 + 字符串 (max-8) + 空字符串 0 = 恰好 max，应通过。
        let at_limit = Value::Array(vec![
            Value::String("x".repeat(max - 8)),
            Value::String(String::new()),
        ]);
        assert!(at_limit.validate().is_ok());

        // 再加一个 8 字节节点即超出。
        let over = Value::Array(vec![
            Value::String("x".repeat(max - 8)),
            Value::String(String::new()),
            Value::Bool(true),
        ]);
        assert_eq!(over.validate(), Err(FeedbackError::PayloadTooLarge));
    }

    /// 扩展级预算含 schema / version 字节，且跨多个扩展是合计值而非逐扩展重置。
    #[test]
    fn extension_budget_includes_schema_and_is_cumulative() {
        let budget = VALUE_MAX_BYTES;
        // schema(2) + version(1) + 字符串预算内，恰好不超。
        let ok = Extension {
            schema: "sc".into(),
            version: "v".into(),
            data: Value::String("x".repeat(budget - 3)),
        };
        assert!(ok.validate().is_ok());

        // 单条略超即拒绝。
        let too_big = Extension {
            schema: "sc".into(),
            version: "v".into(),
            data: Value::String("x".repeat(budget - 2)),
        };
        assert_eq!(too_big.validate(), Err(FeedbackError::PayloadTooLarge));

        // 单条各自合法，但合计超出：证明预算跨扩展累加。
        let mut fb = Feedback::delayed(key(), "m");
        fb.extensions = vec![
            Extension {
                schema: "a".into(),
                version: "1".into(),
                data: Value::String("x".repeat(budget - 8)),
            };
            3
        ];
        assert_eq!(fb.validate(), Err(FeedbackError::PayloadTooLarge));
    }
}
