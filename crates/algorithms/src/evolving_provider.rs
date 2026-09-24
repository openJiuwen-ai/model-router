//! [`EvolvingProvider`]：在线自演进的唯一接入点。
//!
//! 纯计算、无 I/O。拉数据 / 调度 / CAS 写回由 runtime `TrainingJob` 履行。
//! 同样的 [`TrainingBatch`] 必须返回同样的 [`Artifact`]。

use std::sync::Arc;

use openjiuwen_protocol::{Feedback, TrainingError, TrainingPrompt, TRAINING_MAX_PROMPTS};

/// 按 watermark 从 state / 宿主 journal 拉到的增量训练输入，由 DataSelector 组装。
///
/// * `feedbacks`：仅含结果反馈的旧通道，向后兼容，仍然可用。
/// * `prompts`：带「请求 → 决策 → 反馈」三元组的富样本，供无法只凭反馈
///   建模的算法使用（如需要原始 prompt 做 embedding 或监督信号）。
///
/// 两者可同时非空；算法按需读取，缺失时降级（空批次是合法输入）。
#[derive(Clone, Debug, Default)]
pub struct TrainingBatch {
    pub feedbacks: Vec<Feedback>,
    pub prompts: Vec<TrainingPrompt>,
}

impl TrainingBatch {
    /// 挂上一批训练样本。
    pub fn with_prompts(mut self, prompts: Vec<TrainingPrompt>) -> Self {
        self.prompts = prompts;
        self
    }

    /// 是否为空批次：两条通道都无数据。算法必须能处理。
    pub fn is_empty(&self) -> bool {
        self.feedbacks.is_empty() && self.prompts.is_empty()
    }

    /// 样本总数（两条通道合计）。
    pub fn len(&self) -> usize {
        self.feedbacks.len().saturating_add(self.prompts.len())
    }

    /// 严格校验：样本条数上限，逐条校验 prompt 与 feedback。
    ///
    /// # Errors
    ///
    /// 样本条数超限，或任一 prompt / feedback 校验失败时返回 [`TrainingError`]。
    pub fn validate(&self) -> Result<(), TrainingError> {
        if self.prompts.len() > TRAINING_MAX_PROMPTS {
            return Err(TrainingError::TooManyPrompts);
        }
        for prompt in &self.prompts {
            prompt.validate()?;
        }
        for feedback in &self.feedbacks {
            feedback.validate().map_err(TrainingError::Feedback)?;
        }
        Ok(())
    }
}

/// 不可变新参数集快照，可多版本共存。
#[derive(Clone, Debug)]
pub struct Artifact {
    pub kind: String,
    pub payload: Vec<u8>,
}

/// 算法自演进插件契约。与路由侧 `AlgorithmProvider` 对位，但不占路由单槽。
pub trait EvolvingProvider: Send + Sync {
    /// 稳定的低基数名称，用于注册表与遥测。
    fn name(&self) -> &str;

    /// 单步重算。同输入必同输出；不允许 I/O。
    fn fit(&self, batch: &TrainingBatch) -> Arc<Artifact>;
}

#[cfg(test)]
mod tests {
    use super::*;
    use openjiuwen_protocol::{
        RoutingKey, TrainingError, TRAINING_MAX_PROMPTS, TRAINING_MAX_TEXT_BYTES,
    };

    fn key() -> RoutingKey {
        RoutingKey {
            session_id: "s".into(),
            agent_id: "a".into(),
        }
    }

    #[test]
    fn default_batch_is_empty() {
        let batch = TrainingBatch::default();
        assert!(batch.is_empty());
        assert_eq!(batch.len(), 0);
        assert!(batch.validate().is_ok());
    }

    #[test]
    fn prompts_count_toward_length() {
        let batch =
            TrainingBatch::default().with_prompts(vec![TrainingPrompt::new(key()).with_text("t")]);
        assert!(!batch.is_empty());
        assert_eq!(batch.len(), 1);
        assert!(batch.validate().is_ok());
    }

    #[test]
    fn invalid_prompt_fails_batch_validation() {
        let prompt = TrainingPrompt::new(key()).with_text("x".repeat(TRAINING_MAX_TEXT_BYTES + 1));
        let batch = TrainingBatch::default().with_prompts(vec![prompt]);
        assert_eq!(batch.validate(), Err(TrainingError::TextTooLarge));
    }

    #[test]
    fn too_many_prompts_is_rejected() {
        let batch = TrainingBatch::default().with_prompts(
            (0..=TRAINING_MAX_PROMPTS)
                .map(|_| TrainingPrompt::new(key()))
                .collect(),
        );
        assert_eq!(batch.validate(), Err(TrainingError::TooManyPrompts));
    }
}
