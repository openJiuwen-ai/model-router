//! Decision 的可序列化投影，供宿主 / 下一级插件消费。
//!
//! 字段与 [`crate::Decision`] 相同，二者仅在「是否可执行」上有别：
//! `Decision` 是 runtime 内部返回值，`ModelSelection` 是跨边界规格。

use crate::Decision;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelSelection {
    pub selected_model_id: String,
    pub reasoning: String,
    pub is_answer_call: bool,
    pub decision_id: Option<String>,
}

impl From<&Decision> for ModelSelection {
    fn from(d: &Decision) -> Self {
        Self {
            selected_model_id: d.selected_model_id.clone(),
            reasoning: d.reasoning.clone(),
            is_answer_call: d.is_answer_call,
            decision_id: d.decision_id.clone(),
        }
    }
}

impl From<Decision> for ModelSelection {
    fn from(d: Decision) -> Self {
        Self::from(&d)
    }
}
