//! TOML profile 解析。端云差异全部收敛在配置，不在代码分叉。

use std::path::Path;

use openjiuwen_protocol::RouterError;
use serde::Deserialize;

#[derive(Clone, Debug, Deserialize)]
pub struct RouterProfile {
    pub algorithm: String,
    pub state: StateConfig,
    #[serde(default)]
    pub targets: TargetsConfig,
    #[serde(default)]
    pub evolving: Vec<EvolvingJobConfig>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StateConfig {
    /// `memory` | `remote`
    pub backend: String, // 后端类型
    #[serde(default)]
    pub ttl_secs: Option<u64>, // 缓存超时时间
    #[serde(default)]
    pub max_entries: Option<usize>, // 缓存最大条目数
    #[serde(default)]
    pub endpoint: Option<String>, // 远程端点
    #[serde(default)]
    pub timeout_ms: Option<u64>, // 超时时间
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct TargetsConfig {
    #[serde(default)]
    pub models: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct EvolvingJobConfig {
    pub name: String,
    #[serde(default)]
    pub kind: Option<String>,
    #[serde(default)]
    pub slot: Option<String>,
}

impl RouterProfile {
    /// 从文件路径加载配置。
    ///
    /// # Errors
    ///
    /// 文件无法读取或 TOML 无法解析时返回 [`RouterError`]。
    pub fn from_path(path: impl AsRef<Path>) -> Result<Self, RouterError> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path)
            .map_err(|e| RouterError::Config(format!("read {}: {e}", path.display())))?;
        Self::from_toml(&text)
    }

    /// 从 TOML 文本加载配置。
    ///
    /// # Errors
    ///
    /// TOML 无法解析为 [`RouterProfile`] 时返回 [`RouterError`]。
    pub fn from_toml(text: &str) -> Result<Self, RouterError> {
        toml::from_str(text).map_err(|e| RouterError::Config(format!("parse toml: {e}")))
    }
}
