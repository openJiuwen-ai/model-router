//! 状态快照。算法必须能在空视图下降级为冷路由。

/// 累计反馈统计（hint）。字段后续随算法需求扩展。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct FeedbackStats {
    pub sample_count: u64,
}

/// 路由前一次性读入的状态快照。丢失只降质，不阻塞。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct StateView {
    pub affinity: Option<String>, // 亲和性提示
    pub exclusions: Vec<String>,  // 排除列表
    pub stats: FeedbackStats,     // 反馈统计
}

impl StateView {
    /// 创建一个空的状态快照。
    pub fn empty() -> Self {
        Self::default()
    }

    /// 判断状态快照是否为空。
    pub fn is_empty(&self) -> bool {
        self.affinity.is_none() && self.exclusions.is_empty() && self.stats.sample_count == 0
    }
}
