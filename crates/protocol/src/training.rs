//! 训练样本入参。`TrainingBatch` 的富载荷，供 `EvolvingProvider` 消费。
//!
//! 单条 [`Feedback`] 只说明「选了谁、结果如何」，不含「问了什么」（请求）
//! 与「为什么选」（决策）。`TrainingPrompt` 把 `请求 → 决策 → 反馈` 三元组
//! 组装成一条训练样本；三元组均为可选，与反馈侧「缺失表示未知」的约定一致
//! （延迟评价、旧数据、未关联都会缺字段）。
//!
//! 宿主也可以直接给出渲染好的自然语言 `text`，此时算法无需自己拼模板。
//! 两者可同时存在：`text` 面向已渲染的提示词，结构字段面向自建模板。
//!
//! 与状态侧载荷一致：所有上限都是硬约束，超限一律拒绝，绝不无限增长。

use crate::decision::Decision;
use crate::feedback::{Extension, Feedback, FeedbackError, MAX_EXTENSIONS};
use crate::request::{RouteRequest, RoutingKey};

/// 单个训练批次允许的最大样本条数。
pub const TRAINING_MAX_PROMPTS: usize = 1_024;
/// 渲染后 prompt 文本的最大 UTF-8 字节数。
pub const TRAINING_MAX_TEXT_BYTES: usize = 65_536;
/// 单条样本携带的最大消息条数。
pub const TRAINING_MAX_MESSAGES: usize = 256;
/// 单条消息 `content` 的最大 UTF-8 字节数。
pub const TRAINING_MAX_MESSAGE_BYTES: usize = 65_536;

/// 一条训练样本：同一路由键下的一次「请求 → 决策 → 反馈」。
///
/// * `prompt_id`：宿主侧样本关联 id；缺失表示未关联。
/// * `key`：路由键，始终存在，用于把样本归并到同一会话 / agent。
/// * `request`：当时的原始请求（消息与元数据）。
/// * `decision`：当时的决策（选中目标、理由、`route_id`）。
/// * `feedback`：事后的结果反馈；缺失表示评价尚未回传。
/// * `text`：宿主渲染好的 prompt 文本；缺失表示由算法自行拼装。
/// * `extensions`：宿主私有训练载荷，复用 [`Extension`] 版本化预算。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TrainingPrompt {
    pub prompt_id: Option<String>,
    pub key: RoutingKey,
    pub request: Option<RouteRequest>,
    pub decision: Option<Decision>,
    pub feedback: Option<Feedback>,
    pub text: Option<String>,
    pub extensions: Vec<Extension>,
}

impl TrainingPrompt {
    /// 仅带路由键的空样本。
    pub fn new(key: RoutingKey) -> Self {
        Self {
            key,
            ..Self::default()
        }
    }

    /// 挂上原始请求。
    pub fn with_request(mut self, request: RouteRequest) -> Self {
        self.request = Some(request);
        self
    }

    /// 挂上决策。
    pub fn with_decision(mut self, decision: Decision) -> Self {
        self.decision = Some(decision);
        self
    }

    /// 挂上结果反馈。
    pub fn with_feedback(mut self, feedback: Feedback) -> Self {
        self.feedback = Some(feedback);
        self
    }

    /// 挂上宿主已渲染的 prompt 文本。
    pub fn with_text(mut self, text: impl Into<String>) -> Self {
        self.text = Some(text.into());
        self
    }

    /// 是否未携带任何训练信息（仅路由键）。此时对算法无价值。
    pub fn is_empty(&self) -> bool {
        self.request.is_none()
            && self.decision.is_none()
            && self.feedback.is_none()
            && self.text.is_none()
            && self.extensions.is_empty()
    }

    /// 严格校验：prompt 文本长度、消息条数与单条长度、扩展载荷预算。
    ///
    /// # Errors
    ///
    /// 文本、消息或扩展载荷超限，或内嵌反馈校验失败时返回 [`TrainingError`]。
    pub fn validate(&self) -> Result<(), TrainingError> {
        if let Some(text) = &self.text {
            if text.len() > TRAINING_MAX_TEXT_BYTES {
                return Err(TrainingError::TextTooLarge);
            }
        }
        if let Some(request) = &self.request {
            if request.messages.len() > TRAINING_MAX_MESSAGES {
                return Err(TrainingError::TooManyMessages);
            }
            if request
                .messages
                .iter()
                .any(|m| m.content.len() > TRAINING_MAX_MESSAGE_BYTES)
            {
                return Err(TrainingError::MessageTooLarge);
            }
        }
        if let Some(feedback) = &self.feedback {
            feedback.validate().map_err(TrainingError::Feedback)?;
        }
        if self.extensions.len() > MAX_EXTENSIONS {
            return Err(TrainingError::Extension(FeedbackError::PayloadTooLarge));
        }
        let mut count: usize = 0;
        let mut bytes: usize = 0;
        for ext in &self.extensions {
            ext.validate().map_err(TrainingError::Extension)?;
            crate::feedback::add_payload_budget(&mut bytes, ext.schema.len())
                .map_err(TrainingError::Extension)?;
            crate::feedback::add_payload_budget(&mut bytes, ext.version.len())
                .map_err(TrainingError::Extension)?;
            ext.data
                .validate_inner(1, &mut count, &mut bytes)
                .map_err(TrainingError::Extension)?;
        }
        Ok(())
    }
}

/// 训练入参校验错误。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TrainingError {
    /// 样本条数超过 [`TRAINING_MAX_PROMPTS`]。
    TooManyPrompts,
    /// 渲染后 prompt 文本超过 [`TRAINING_MAX_TEXT_BYTES`]。
    TextTooLarge,
    /// 单条样本消息数超过 [`TRAINING_MAX_MESSAGES`]。
    TooManyMessages,
    /// 单条消息 `content` 超过 [`TRAINING_MAX_MESSAGE_BYTES`]。
    MessageTooLarge,
    /// 内嵌反馈不合规，复用反馈侧同一套错误。
    Feedback(FeedbackError),
    /// 扩展载荷不合规，复用反馈侧同一套错误。
    Extension(FeedbackError),
}

impl std::fmt::Display for TrainingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::TooManyPrompts => write!(f, "training prompts exceed {TRAINING_MAX_PROMPTS}"),
            Self::TextTooLarge => {
                write!(
                    f,
                    "training prompt text exceeds {TRAINING_MAX_TEXT_BYTES} bytes"
                )
            }
            Self::TooManyMessages => {
                write!(f, "training prompt messages exceed {TRAINING_MAX_MESSAGES}")
            }
            Self::MessageTooLarge => write!(
                f,
                "training prompt message content exceeds {TRAINING_MAX_MESSAGE_BYTES} bytes"
            ),
            Self::Feedback(err) => write!(f, "training prompt feedback: {err}"),
            Self::Extension(err) => write!(f, "training prompt extension: {err}"),
        }
    }
}

impl std::error::Error for TrainingError {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feedback::Value;
    use crate::request::Message;

    fn key() -> RoutingKey {
        RoutingKey {
            session_id: "s".into(),
            agent_id: "a".into(),
        }
    }

    #[test]
    fn empty_prompt_is_detected() {
        assert!(TrainingPrompt::new(key()).is_empty());
        assert!(!TrainingPrompt::new(key()).with_text("hi").is_empty());
        assert!(!TrainingPrompt::new(key())
            .with_feedback(Feedback::ok(key(), "m", 1))
            .is_empty());
    }

    #[test]
    fn builder_carries_request_decision_feedback() {
        let prompt = TrainingPrompt::new(key())
            .with_request(RouteRequest {
                messages: vec![Message {
                    role: "user".into(),
                    content: "hello".into(),
                    ..Message::default()
                }],
                ..RouteRequest::default()
            })
            .with_decision(Decision::answer("m", "why"))
            .with_feedback(Feedback::ok(key(), "m", 12))
            .with_text("rendered");
        assert_eq!(prompt.request.as_ref().unwrap().messages.len(), 1);
        assert_eq!(prompt.decision.as_ref().unwrap().selected_model_id, "m");
        assert_eq!(prompt.feedback.as_ref().unwrap().selected_model_id, "m");
        assert!(prompt.validate().is_ok());
    }

    #[test]
    fn text_and_message_bounds() {
        let too_long =
            TrainingPrompt::new(key()).with_text("x".repeat(TRAINING_MAX_TEXT_BYTES + 1));
        assert_eq!(too_long.validate(), Err(TrainingError::TextTooLarge));

        let mut request = RouteRequest::default();
        request.messages = vec![
            Message {
                role: "user".into(),
                content: "ok".into(),
                ..Message::default()
            };
            TRAINING_MAX_MESSAGES + 1
        ];
        assert_eq!(
            TrainingPrompt::new(key()).with_request(request).validate(),
            Err(TrainingError::TooManyMessages)
        );

        let mut request = RouteRequest::default();
        request.messages = vec![Message {
            role: "user".into(),
            content: "x".repeat(TRAINING_MAX_MESSAGE_BYTES + 1),
            ..Message::default()
        }];
        assert_eq!(
            TrainingPrompt::new(key()).with_request(request).validate(),
            Err(TrainingError::MessageTooLarge)
        );
    }

    #[test]
    fn embedded_feedback_is_validated() {
        let mut bad = Feedback::ok(key(), "m", 1);
        bad.version = 999;
        assert_eq!(
            TrainingPrompt::new(key()).with_feedback(bad).validate(),
            Err(TrainingError::Feedback(FeedbackError::UnsupportedVersion))
        );
    }

    #[test]
    fn extension_budget_is_shared_across_entries() {
        let ext = Extension {
            schema: "vendor.train".into(),
            version: "1".into(),
            data: Value::String("x".repeat(40_000)),
        };
        let mut prompt = TrainingPrompt::new(key());
        prompt.extensions = vec![ext; 2];
        assert_eq!(
            prompt.validate(),
            Err(TrainingError::Extension(FeedbackError::PayloadTooLarge))
        );
    }
}
