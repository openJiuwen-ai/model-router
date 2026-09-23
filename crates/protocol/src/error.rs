//! 路由器错误。装配期错误在启动时暴露；决策期错误可回退。

use std::error::Error;
use std::fmt;

/// 路由器错误类型。
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RouterError {
    Config(String),    // 配置错误
    Algorithm(String), // 算法错误
    State(String),     // 状态错误
    NoTarget,          // 没有可用目标
}

/// 实现错误显示。
impl fmt::Display for RouterError {
    /// 实现错误显示。
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Config(msg) => write!(f, "config: {msg}"), // 配置错误
            Self::Algorithm(msg) => write!(f, "algorithm: {msg}"), // 算法错误
            Self::State(msg) => write!(f, "state: {msg}"),   // 状态错误
            Self::NoTarget => write!(f, "no available target after exclusions"), // 没有可用目标
        }
    }
}

/// 实现错误转换。
impl Error for RouterError {}
