//! TrainingJob / DataSelector / PublishPlan。后台调度 + CAS 写回。骨架为类型占位。

use std::sync::Arc;

use openjiuwen_algorithms::{Artifact, EvolvingProvider, TrainingBatch};
use openjiuwen_protocol::TrainingPrompt;

/// 按 watermark 从 state / 宿主 journal 拉增量训练输入。骨架返回空 batch。
///
/// 数据来源分两条通道，语义不同：
///
/// * **feedbacks**：来自 `StateProvider`，只有「选了谁、结果如何」。state 是
///   hint 层，天然只存聚合反馈，**不保留原始请求与决策**。
/// * **prompts**：`请求 → 决策 → 反馈` 三元组。runtime 在 `route` 时不落盘
///   决策与请求，因此这条通道必须由**宿主 journal**提供——宿主在调用模型后
///   生成本次三元组，交由训练调度器 `set_prompts` 传入。这也是唯一能拿到
///   原始 prompt 文本的地方。
///
/// 两条通道可并存；`select` 只做透传与批量校验，不做字段拼装。
#[derive(Default)]
pub struct DataSelector {
    pub watermark_key: String,
    pub min_samples: u64,
    /// 宿主预组装好的训练样本。为空则只走 `feedbacks` 通道。
    pub prompts: Vec<TrainingPrompt>,
}

impl DataSelector {
    /// 设置宿主 journal 提供的训练样本。
    pub fn with_prompts(mut self, prompts: Vec<TrainingPrompt>) -> Self {
        self.prompts = prompts;
        self
    }

    pub fn select(&self) -> TrainingBatch {
        let mut batch = TrainingBatch::default();
        batch.prompts = self.prompts.clone();
        batch
    }
}

/// 写回计划：目标槽 + 期望版本。
#[derive(Clone, Debug)]
pub struct PublishPlan {
    pub slot: String,
    pub expected_version: u64,
}

/// 训练任务的执行体：拉数据 → EvolvingProvider.fit → CAS 写回。
pub struct TrainingJob {
    pub name: String,
    pub selector: DataSelector,
    pub publish: PublishPlan,
}

impl TrainingJob {
    /// 骨架：选出空 batch、fit、丢弃工件。真实写回走 StateProvider::publish。
    pub fn run_once(&self, evolving: &dyn EvolvingProvider) -> Arc<Artifact> {
        let batch = self.selector.select();
        evolving.fit(&batch)
    }
}
