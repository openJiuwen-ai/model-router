//! Trigger trait + TriggerSpec。判定在 runtime，不进算法。

use std::collections::HashMap;
use std::time::{Duration, Instant};

use openjiuwen_protocol::FeedbackStats;

/// 触发规格：声明式、可序列化、TOML 装配。
#[derive(Clone, Debug)]
pub enum TriggerSpec {
    Startup,
    Delay {
        seconds: u64,
    },
    Interval {
        seconds: u64,
    },
    Cron {
        expr: String,
    },
    Threshold {
        metric: String,
        op: Cmp,
        value: f64,
    },
    Event {
        kind: String,
    },
    Composite {
        operator: AndOr,
        children: Vec<TriggerSpec>,
    },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Cmp {
    Eq,
    Gt,
    Lt,
    Ge,
    Le,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AndOr {
    And,
    Or,
}

/// Trigger 能看到的运行时上下文。由 runtime 组装，trigger 不持状态。
pub struct TriggerContext<'a> {
    pub now: Instant,
    pub last_fire: &'a HashMap<String, Instant>,
    pub counters: &'a FeedbackStats,
    pub events: &'a [String],
}

/// 把时钟与信号统一成一个判定接口。
pub trait Trigger: Send + Sync {
    fn name(&self) -> &str;
    fn next_due(&self, now: Instant) -> Option<Duration>;
    fn satisfied(&self, ctx: &TriggerContext<'_>) -> bool;
}

/// 注册表：trigger_id → (TriggerSpec, job 名)。骨架只保存规格。
#[derive(Default)]
pub struct TriggerRegistry {
    pub entries: HashMap<String, TriggerSpec>,
}

impl TriggerRegistry {
    pub fn register(&mut self, id: impl Into<String>, spec: TriggerSpec) {
        self.entries.insert(id.into(), spec);
    }
}
