//! 进程内内存实现（TTL + LRU 上界）。骨架阶段用容量上限代替完整 LRU。

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use openjiuwen_protocol::{Feedback, Outcome, RoutingKey, StateView};

use crate::{CasConflict, StateProvider};

#[derive(Clone)]
struct Entry {
    view: StateView,
    expires_at: Instant,
}

/// 端侧默认 state 槽实现：堆内存、可丢失。profile 中 `backend = "memory"`。
pub struct MemoryState {
    ttl: Duration,
    max_entries: usize,
    inner: Mutex<HashMap<RoutingKey, Entry>>,
}

impl MemoryState {
    pub fn new(ttl: Duration, max_entries: usize) -> Self {
        Self {
            ttl,
            max_entries: max_entries.max(1),
            inner: Mutex::new(HashMap::new()),
        }
    }

    pub fn with_defaults() -> Self {
        Self::new(Duration::from_secs(300), 1024)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_calls_update_state() {
        let state = MemoryState::default();
        let key = RoutingKey::default();
        state.report(Feedback::delayed(key.clone(), "late"));
        assert_eq!(state.snapshot(&key), StateView::empty());
        let success = Feedback::ok(key.clone(), "first", 1);
        state.report(success.clone());
        let before = state.snapshot(&key);
        state.report(Feedback::delayed(key.clone(), "late"));
        assert_eq!(state.snapshot(&key), before);
        state.report(success);
        assert_eq!(state.snapshot(&key).stats.sample_count, 2);
        for outcome in [Outcome::Overflow, Outcome::Unavailable, Outcome::Rejected] {
            let mut fb = Feedback::ok(key.clone(), "failed", 1);
            fb.call.as_mut().unwrap().outcome = outcome;
            state.report(fb);
        }
        let view = state.snapshot(&key);
        assert_eq!(view.stats.sample_count, 5);
        assert_eq!(view.affinity.as_deref(), Some("first"));
        assert_eq!(view.exclusions, vec!["failed"]);
    }
}

impl Default for MemoryState {
    fn default() -> Self {
        Self::with_defaults()
    }
}

impl StateProvider for MemoryState {
    fn snapshot(&self, key: &RoutingKey) -> StateView {
        let now = Instant::now();
        let mut map = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        match map.get(key) {
            Some(entry) if entry.expires_at > now => entry.view.clone(),
            Some(_) => {
                map.remove(key);
                StateView::empty()
            }
            None => StateView::empty(),
        }
    }

    // report 按 Feedback.key（session_id + agent_id）更新内存里的一份 StateView，给下次 snapshot 用。
    fn report(&self, feedback: Feedback) {
        let Some(call) = &feedback.call else {
            return;
        };
        let now = Instant::now();
        let mut map = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        // 如果内存里超过最大条目数，并且反馈的 key 不在内存里，则移除最旧的条目。
        if map.len() >= self.max_entries && !map.contains_key(&feedback.key) {
            if let Some(oldest) = map.keys().next().cloned() {
                map.remove(&oldest);
            }
        }
        // 插入或更新 entry。
        let entry = map.entry(feedback.key.clone()).or_insert_with(|| Entry {
            view: StateView::empty(),
            expires_at: now + self.ttl,
        });
        // 更新过期时间。
        entry.expires_at = now + self.ttl;
        // 更新样本计数。
        entry.view.stats.sample_count = entry.view.stats.sample_count.saturating_add(1);
        // 根据反馈结果更新 StateView。
        match call.outcome {
            // 溢出或不可用则添加到排除列表。
            Outcome::Overflow | Outcome::Unavailable => {
                if !entry.view.exclusions.contains(&feedback.selected_model_id) {
                    entry.view.exclusions.push(feedback.selected_model_id);
                }
            }
            // 成功则更新亲和。
            Outcome::Ok => {
                entry.view.affinity = Some(feedback.selected_model_id);
            }
            // 拒绝则不更新。
            Outcome::Rejected => {}
        }
    }

    fn publish(&self, _slot: &str, _artifact: &[u8], _ver: u64) -> Result<(), CasConflict> {
        Ok(())
    }
}
