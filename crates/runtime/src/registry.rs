//! 算法池：候选目录 · 单槽选一 · 可热插拔。

use openjiuwen_algorithms::test_algo::routing as samples;
use openjiuwen_algorithms::AlgorithmProvider;
use openjiuwen_protocol::RouterError;

/// 按名称从示意算法池取出一个实例。未命中则报装配错误。
pub fn create_algorithm(name: &str) -> Result<Box<dyn AlgorithmProvider>, RouterError> {
    match name {
        #[cfg(feature = "algo-passthrough")]
        "passthrough" => Ok(Box::new(samples::passthrough::Passthrough)),
        #[cfg(feature = "algo-weighted")]
        "weighted" => Ok(Box::new(samples::weighted::Weighted)),
        #[cfg(feature = "algo-rule_cascade")]
        "rule_cascade" => Ok(Box::new(samples::rule_cascade::RuleCascade)),
        #[cfg(feature = "algo-stage_router")]
        "stage_router" => Ok(Box::new(openjiuwen_algorithms::stage_router::StageRouter)),
        #[cfg(feature = "algo-signal")]
        "signal" => Ok(Box::new(samples::signal::Signal)),
        #[cfg(feature = "algo-ensemble")]
        "ensemble" => Ok(Box::new(samples::ensemble::Ensemble)),
        other => Err(RouterError::Config(format!(
            "unknown or disabled algorithm: {other}"
        ))),
    }
}
