//! 路由请求与目标集合。

use crate::state_query::StateQuery;

/// 供路由算法观察的工具调用。`command` 仅在 shell 类工具提供命令时存在。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ToolCall {
    pub name: String,
    pub command: Option<String>,
}

/// 单条对话消息。协议层只搬运内容与路由所需的工具调用信号。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Message {
    pub role: String,    // 角色
    pub content: String, // 内容
    pub tool_calls: Vec<ToolCall>,
}

/// 请求元数据。`session_id` / `agent_id` 构成 [`RoutingKey`]。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RequestMetadata {
    pub session_id: Option<String>, // 会话id
    pub agent_id: Option<String>, // agent id
}

impl RequestMetadata {
    pub fn routing_key(&self) -> RoutingKey {
        RoutingKey {
            session_id: self.session_id.clone().unwrap_or_default(),
            agent_id: self.agent_id.clone().unwrap_or_default(),
        }
    }
}

/// 状态快照的键空间。
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct RoutingKey {
    pub session_id: String,
    pub agent_id: String,
}

/// 本次可选目标（语义名）。排除集由请求与状态 hint 共同决定。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct TargetSet {
    pub models: Vec<String>,
}

impl TargetSet {
    /// 创建一个新的目标集合。
    pub fn new(models: impl IntoIterator<Item = impl Into<String>>) -> Self {
        Self {
            models: models.into_iter().map(Into::into).collect(),
        }
    }

    pub fn first(&self) -> Option<&str> {
        self.models.first().map(String::as_str)
    }

    /// 剔除 exclusions 后的目标子集；保持原顺序。
    pub fn without(&self, exclusions: &[String]) -> Self {
        Self {
            models: self
                .models
                .iter()
                .filter(|m| !exclusions.iter().any(|e| e == *m))
                .cloned()
                .collect(),
        }
    }
}

/// 宿主侧每请求输入。`cache_affinity` 是 KV cache 驻留模型 hint。
///
/// `state_query` 为可选的检索意图：`None`（默认）时 runtime 走
/// `StateProvider::snapshot`，与旧版行为完全一致；`Some` 时改走
/// `StateProvider::query`。
///
/// 注意：因 [`StateQuery`] 携带浮点向量，本结构体只派生 `PartialEq`。
#[derive(Clone, Debug, Default, PartialEq)]
pub struct RouteHint {
    pub cache_affinity: Option<String>, // 缓存亲和性提示
    pub state_query: Option<StateQuery>, // 可选的状态检索意图
}

impl RouteHint {
    /// 创建带检索意图的 hint。
    pub fn with_state_query(mut self, query: StateQuery) -> Self {
        self.state_query = Some(query);
        self
    }
}

/// 路由入参。`exclusions` 由宿主重试逻辑填写。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RouteRequest {
    pub messages: Vec<Message>, // 消息
    pub metadata: RequestMetadata, // 请求元数据
    pub exclusions: Vec<String>, // 排除列表
}

impl RouteRequest {
    /// 获取路由键。
    pub fn routing_key(&self) -> RoutingKey {
        self.metadata.routing_key()    // 获取请求元数据的路由键。
    }
}
