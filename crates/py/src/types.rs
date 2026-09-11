//! 跨 PyO3 边界的协议类型。蓝图：RouteRequest / ModelSelection / Feedback。

use pyo3::prelude::*;
use pyo3::types::PyType;

use openjiuwen_protocol::{
    Decision, Feedback, FeedbackStats, Message, ModelSelection, RequestMetadata, RouteHint,
    RouteRequest, RoutingKey, StateView,
};

use crate::convert;

#[pyclass(name = "Message", get_all, set_all)]
#[derive(Clone, Debug)]
pub struct PyMessage {
    pub role: String,
    pub content: String,
}

#[pymethods]
impl PyMessage {
    #[new]
    #[pyo3(signature = (role, content))]
    fn new(role: String, content: String) -> Self {
        Self { role, content }
    }
}

impl From<&PyMessage> for Message {
    fn from(m: &PyMessage) -> Self {
        Self {
            role: m.role.clone(),
            content: m.content.clone(),
        }
    }
}

impl From<&Message> for PyMessage {
    fn from(m: &Message) -> Self {
        Self {
            role: m.role.clone(),
            content: m.content.clone(),
        }
    }
}

#[pyclass(name = "RequestMetadata", get_all, set_all)]
#[derive(Clone, Debug, Default)]
pub struct PyRequestMetadata {
    pub session_id: Option<String>,
    pub agent_id: Option<String>,
}

#[pymethods]
impl PyRequestMetadata {
    #[new]
    #[pyo3(signature = (session_id=None, agent_id=None))]
    fn new(session_id: Option<String>, agent_id: Option<String>) -> Self {
        Self {
            session_id,
            agent_id,
        }
    }

    fn routing_key(&self) -> PyRoutingKey {
        PyRoutingKey::from(&self.native().routing_key())
    }
}

impl PyRequestMetadata {
    pub fn native(&self) -> RequestMetadata {
        RequestMetadata {
            session_id: self.session_id.clone(),
            agent_id: self.agent_id.clone(),
        }
    }
}

impl From<&RequestMetadata> for PyRequestMetadata {
    fn from(m: &RequestMetadata) -> Self {
        Self {
            session_id: m.session_id.clone(),
            agent_id: m.agent_id.clone(),
        }
    }
}

#[pyclass(name = "RoutingKey", get_all, set_all)]
#[derive(Clone, Debug, Default)]
pub struct PyRoutingKey {
    pub session_id: String,
    pub agent_id: String,
}

#[pymethods]
impl PyRoutingKey {
    #[new]
    #[pyo3(signature = (session_id=None, agent_id=None))]
    fn new(session_id: Option<String>, agent_id: Option<String>) -> Self {
        Self {
            session_id: session_id.unwrap_or_default(),
            agent_id: agent_id.unwrap_or_default(),
        }
    }
}

impl PyRoutingKey {
    pub fn native(&self) -> RoutingKey {
        RoutingKey {
            session_id: self.session_id.clone(),
            agent_id: self.agent_id.clone(),
        }
    }
}

impl From<&RoutingKey> for PyRoutingKey {
    fn from(k: &RoutingKey) -> Self {
        Self {
            session_id: k.session_id.clone(),
            agent_id: k.agent_id.clone(),
        }
    }
}

#[pyclass(name = "RouteHint", get_all, set_all)]
#[derive(Clone, Debug, Default)]
pub struct PyRouteHint {
    pub cache_affinity: Option<String>,
}

#[pymethods]
impl PyRouteHint {
    #[new]
    #[pyo3(signature = (cache_affinity=None))]
    fn new(cache_affinity: Option<String>) -> Self {
        Self { cache_affinity }
    }
}

impl PyRouteHint {
    pub fn native(&self) -> RouteHint {
        RouteHint {
            cache_affinity: self.cache_affinity.clone(),
        }
    }
}

#[pyclass(name = "RouteRequest")]
#[derive(Clone, Debug, Default)]
pub struct PyRouteRequest {
    #[pyo3(get, set)]
    pub messages: Vec<PyMessage>,
    #[pyo3(get, set)]
    pub metadata: PyRequestMetadata,
    #[pyo3(get, set)]
    pub exclusions: Vec<String>,
}

#[pymethods]
impl PyRouteRequest {
    #[new]
    #[pyo3(signature = (messages=None, metadata=None, exclusions=None))]
    fn new(
        messages: Option<Vec<PyMessage>>,
        metadata: Option<PyRequestMetadata>,
        exclusions: Option<Vec<String>>,
    ) -> Self {
        Self {
            messages: messages.unwrap_or_default(),
            metadata: metadata.unwrap_or_default(),
            exclusions: exclusions.unwrap_or_default(),
        }
    }

    fn routing_key(&self) -> PyRoutingKey {
        PyRoutingKey::from(&self.native().routing_key())
    }
}

impl PyRouteRequest {
    pub fn native(&self) -> RouteRequest {
        RouteRequest {
            messages: self.messages.iter().map(Message::from).collect(),
            metadata: self.metadata.native(),
            exclusions: self.exclusions.clone(),
        }
    }

    pub fn from_native(req: &RouteRequest) -> Self {
        Self {
            messages: req.messages.iter().map(PyMessage::from).collect(),
            metadata: PyRequestMetadata::from(&req.metadata),
            exclusions: req.exclusions.clone(),
        }
    }
}

#[pyclass(name = "FeedbackStats", get_all)]
#[derive(Clone, Debug, Default)]
pub struct PyFeedbackStats {
    pub sample_count: u64,
}

#[pymethods]
impl PyFeedbackStats {
    #[new]
    #[pyo3(signature = (sample_count=0))]
    fn new(sample_count: u64) -> Self {
        Self { sample_count }
    }
}

impl From<&FeedbackStats> for PyFeedbackStats {
    fn from(s: &FeedbackStats) -> Self {
        Self {
            sample_count: s.sample_count,
        }
    }
}

#[pyclass(name = "StateView", get_all)]
#[derive(Clone, Debug, Default)]
pub struct PyStateView {
    pub affinity: Option<String>,
    pub exclusions: Vec<String>,
    pub stats: PyFeedbackStats,
}

#[pymethods]
impl PyStateView {
    #[new]
    #[pyo3(signature = (affinity=None, exclusions=None, stats=None))]
    fn new(
        affinity: Option<String>,
        exclusions: Option<Vec<String>>,
        stats: Option<PyFeedbackStats>,
    ) -> Self {
        Self {
            affinity,
            exclusions: exclusions.unwrap_or_default(),
            stats: stats.unwrap_or_default(),
        }
    }
}

impl PyStateView {
    pub fn native(&self) -> StateView {
        StateView {
            affinity: self.affinity.clone(),
            exclusions: self.exclusions.clone(),
            stats: FeedbackStats {
                sample_count: self.stats.sample_count,
            },
        }
    }
}

impl From<&StateView> for PyStateView {
    fn from(v: &StateView) -> Self {
        Self {
            affinity: v.affinity.clone(),
            exclusions: v.exclusions.clone(),
            stats: PyFeedbackStats::from(&v.stats),
        }
    }
}

#[pyclass(name = "RouteContext", get_all)]
#[derive(Clone, Debug)]
pub struct PyRouteContext {
    /// 与 Python 内置算法兼容：直接是模型名列表，不是 TargetSet 包装。
    pub targets: Vec<String>,
    pub view: PyStateView,
    pub seed: u64,
}

#[pyclass(name = "ModelSelection", get_all)]
#[derive(Clone, Debug)]
pub struct PyModelSelection {
    pub selected_model_id: String,
    pub reasoning: String,
    pub is_answer_call: bool,
    pub decision_id: Option<String>,
}

#[pymethods]
impl PyModelSelection {
    #[new]
    #[pyo3(signature = (selected_model_id, reasoning, is_answer_call=true))]
    fn new(selected_model_id: String, reasoning: String, is_answer_call: bool) -> Self {
        Self {
            selected_model_id,
            reasoning,
            is_answer_call,
            decision_id: None,
        }
    }

    /// 蓝图样例里的 `decision.target` 别名。
    #[getter]
    fn target(&self) -> &str {
        &self.selected_model_id
    }
}

impl PyModelSelection {
    pub fn from_decision(d: &Decision) -> Self {
        let sel = ModelSelection::from(d);
        Self {
            selected_model_id: sel.selected_model_id,
            reasoning: sel.reasoning,
            is_answer_call: sel.is_answer_call,
            decision_id: sel.decision_id,
        }
    }

    pub fn to_decision(&self) -> Decision {
        Decision {
            selected_model_id: self.selected_model_id.clone(),
            reasoning: self.reasoning.clone(),
            is_answer_call: self.is_answer_call,
            decision_id: self.decision_id.clone(),
        }
    }
}

#[pyclass(name = "Feedback")]
#[derive(Clone, Debug)]
pub struct PyFeedback {
    pub inner: Feedback,
}

#[pyclass(name = "CallFeedback", get_all)]
#[derive(Clone, Debug)]
pub struct PyCallFeedback {
    pub outcome: String,
    pub latency_ms: Option<u64>,
    pub cache_valid: Option<bool>,
}

#[pymethods]
impl PyCallFeedback {
    #[new]
    #[pyo3(signature = (outcome, latency_ms=None, cache_valid=None))]
    fn new(
        outcome: &str,
        latency_ms: Option<&Bound<'_, PyAny>>,
        cache_valid: Option<bool>,
    ) -> PyResult<Self> {
        let latency_ms = latency_ms.map(convert::unsigned_integer).transpose()?;
        Ok(Self {
            outcome: outcome_name(convert::parse_outcome(outcome)?).into(),
            latency_ms,
            cache_valid,
        })
    }
}

pub fn outcome_name(outcome: openjiuwen_protocol::Outcome) -> &'static str {
    use openjiuwen_protocol::Outcome::*;
    match outcome {
        Ok => "ok",
        Overflow => "overflow",
        Unavailable => "unavailable",
        Rejected => "rejected",
    }
}

#[pyclass(name = "Extension")]
#[derive(Clone, Debug)]
pub struct PyExtension {
    pub inner: openjiuwen_protocol::Extension,
}

#[pymethods]
impl PyExtension {
    #[new]
    fn new(schema: String, version: String, data: &Bound<'_, PyAny>) -> PyResult<Self> {
        let inner = openjiuwen_protocol::Extension {
            schema,
            version,
            data: convert::extract_json(data)?,
        };
        inner.validate().map_err(convert::feedback_error)?;
        Ok(Self { inner })
    }
    #[getter]
    fn schema(&self) -> &str {
        &self.inner.schema
    }
    #[getter]
    fn version(&self) -> &str {
        &self.inner.version
    }
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        convert::json_to_py(py, &self.inner.data)
    }
}

#[pymethods]
impl PyFeedback {
    #[new]
    #[pyo3(signature = (key, selected_model_id, outcome=None, latency_ms=None, cache_valid=None, **kwargs))]
    fn new(
        key: &Bound<'_, PyAny>,
        selected_model_id: String,
        outcome: Option<&str>,
        latency_ms: Option<&Bound<'_, PyAny>>,
        cache_valid: Option<bool>,
        kwargs: Option<&Bound<'_, pyo3::types::PyDict>>,
    ) -> PyResult<Self> {
        // `None` 即“未提供”哨兵：与 dict 路径的 `Some(v) if !v.is_none()` 判定保持一致。
        // 不能用“是否等于默认值”判断，否则显式传入 outcome="ok" / latency_ms=0 会被当成未提供。
        let legacy_latency = latency_ms.map(convert::unsigned_integer).transpose()?;
        let legacy_provided =
            outcome.is_some() || legacy_latency.is_some() || cache_valid.is_some();
        let d = match kwargs {
            Some(d) => d.copy()?,
            None => pyo3::types::PyDict::new(key.py()),
        };
        d.set_item("key", key)?;
        d.set_item("selected_model_id", selected_model_id)?;
        if d.contains("call")? {
            // 新旧两种表达互斥；显式 `call=None` 表示延迟反馈，不写旧字段。
            if legacy_provided {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "call cannot be combined with legacy call fields",
                ));
            }
        } else {
            d.set_item("outcome", outcome.unwrap_or("ok"))?;
            d.set_item("latency_ms", legacy_latency.unwrap_or(0))?;
            d.set_item("cache_valid", cache_valid)?;
        }
        Self::from_dict(&d)
    }
    #[staticmethod]
    fn from_dict(d: &Bound<'_, pyo3::types::PyDict>) -> PyResult<Self> {
        Ok(Self {
            inner: convert::extract_feedback(d.as_any())?,
        })
    }
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        convert::feedback_to_dict(py, &self.inner)
    }
    #[classmethod]
    #[pyo3(signature = (decision, latency_ms, *, key=None, session_id=None, agent_id=None, selected_model_id=None, cache_valid=None, outcome="ok"))]
    fn ok(
        _cls: &Bound<'_, PyType>,
        decision: Bound<'_, PyAny>,
        latency_ms: &Bound<'_, PyAny>,
        key: Option<Bound<'_, PyAny>>,
        session_id: Option<String>,
        agent_id: Option<String>,
        selected_model_id: Option<String>,
        cache_valid: Option<bool>,
        outcome: &str,
    ) -> PyResult<Self> {
        let latency_ms = convert::unsigned_integer(latency_ms)?;
        let model = match selected_model_id {
            Some(m) => m,
            None => convert::selection_model_id(&decision)?,
        };
        let key = match key {
            Some(k) => convert::extract_routing_key(&k)?,
            None => RoutingKey {
                session_id: session_id.unwrap_or_default(),
                agent_id: agent_id.unwrap_or_default(),
            },
        };
        let mut inner = Feedback::ok(key, model, latency_ms);
        inner.call.as_mut().unwrap().outcome = convert::parse_outcome(outcome)?;
        inner.call.as_mut().unwrap().cache_valid = cache_valid;
        inner.decision_id = if let Ok(d) = decision.downcast::<pyo3::types::PyDict>() {
            d.get_item("decision_id")?
                .map(|v| v.extract())
                .transpose()?
                .flatten()
        } else if decision.hasattr("decision_id")? {
            decision.getattr("decision_id")?.extract()?
        } else {
            None
        };
        Ok(Self { inner })
    }
    #[getter]
    fn key(&self) -> PyRoutingKey {
        PyRoutingKey::from(&self.inner.key)
    }
    #[setter]
    fn set_key(&mut self, value: PyRoutingKey) {
        self.inner.key = value.native();
    }
    #[getter]
    fn selected_model_id(&self) -> &str {
        &self.inner.selected_model_id
    }
    #[setter]
    fn set_selected_model_id(&mut self, value: String) {
        self.inner.selected_model_id = value;
    }
    #[getter]
    fn version(&self) -> u32 {
        self.inner.version
    }
    #[getter]
    fn event_id(&self) -> Option<String> {
        self.inner.event_id.clone()
    }
    #[setter]
    fn set_event_id(&mut self, value: Option<String>) {
        self.inner.event_id = value;
    }
    #[getter]
    fn decision_id(&self) -> Option<String> {
        self.inner.decision_id.clone()
    }
    #[setter]
    fn set_decision_id(&mut self, value: Option<String>) {
        self.inner.decision_id = value;
    }
    #[getter]
    fn observed_at_ms(&self) -> Option<u64> {
        self.inner.observed_at_ms
    }
    #[getter]
    fn call(&self) -> Option<PyCallFeedback> {
        self.inner.call.as_ref().map(|c| PyCallFeedback {
            outcome: outcome_name(c.outcome).into(),
            latency_ms: c.latency_ms,
            cache_valid: c.cache_valid,
        })
    }
    #[getter]
    fn extensions(&self) -> Vec<PyExtension> {
        self.inner
            .extensions
            .iter()
            .cloned()
            .map(|inner| PyExtension { inner })
            .collect()
    }
    #[getter]
    fn outcome(&self) -> Option<&str> {
        self.inner.call.as_ref().map(|c| outcome_name(c.outcome))
    }
    #[setter]
    fn set_outcome(&mut self, value: &str) -> PyResult<()> {
        let outcome = convert::parse_outcome(value)?;
        self.legacy_call()?.outcome = outcome;
        Ok(())
    }
    #[getter]
    fn latency_ms(&self) -> Option<u64> {
        self.inner.call.as_ref().and_then(|c| c.latency_ms)
    }
    #[setter]
    fn set_latency_ms(&mut self, value: &Bound<'_, PyAny>) -> PyResult<()> {
        let value = convert::unsigned_integer(value)?;
        self.legacy_call()?.latency_ms = Some(value);
        Ok(())
    }
    #[getter]
    fn cache_valid(&self) -> Option<bool> {
        self.inner.call.as_ref().and_then(|c| c.cache_valid)
    }
    #[setter]
    fn set_cache_valid(&mut self, value: Option<bool>) -> PyResult<()> {
        self.legacy_call()?.cache_valid = value;
        Ok(())
    }
}

impl PyFeedback {
    fn legacy_call(&mut self) -> PyResult<&mut openjiuwen_protocol::CallFeedback> {
        self.inner
            .call
            .as_mut()
            .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("feedback has no call"))
    }
    pub fn from_native(fb: &Feedback) -> Self {
        Self { inner: fb.clone() }
    }
    pub fn native(&self) -> PyResult<Feedback> {
        self.inner.validate().map_err(convert::feedback_error)?;
        Ok(self.inner.clone())
    }
}
