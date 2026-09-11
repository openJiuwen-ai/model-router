//! 最终路由决策。字段语义与 Switchyard 的 `Decision` 同构。

/// 算法返回的最终决策。`reasoning` 必填——每个决策可解释。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    /// 选中目标的语义名，与 [`crate::TargetSet`] 对齐。
    pub selected_model_id: String,
    /// 决策理由（必填）。
    pub reasoning: String,
    /// 是否应答调用。本架构已收敛为仅应答，默认 `true`。
    pub is_answer_call: bool,
    /// runtime 生成的唯一决策 id；算法对象无需自己生成。
    pub decision_id: Option<String>,
}

impl Decision {
    /// 创建一个应答决策。
    pub fn answer(selected_model_id: impl Into<String>, reasoning: impl Into<String>) -> Self {
        Self {
            selected_model_id: selected_model_id.into(), // 选中目标的语义名
            reasoning: reasoning.into(),                 // 决策理由
            is_answer_call: true,                        // 默认应答
            decision_id: None,                           // 由 runtime 填充
        }
    }
}
