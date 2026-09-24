//! L5 PyO3 绑定。Python 宿主经 `_openjiuwen` 调用 runtime::Router。
//!
//! 薄门面：类型转换 + 同步调用；路由逻辑全部在 Rust 内核。
//! 云侧 async 由 `python/openjiuwen` 再包一层。

mod adapter;
mod convert;
mod error;
mod state_adapter;
mod types;

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;

use openjiuwen_protocol::TargetSet;
use openjiuwen_runtime::{registry, KvCacheCoordinator, Router};
use openjiuwen_state::StateProvider;

use crate::convert::{extract_hint, extract_request, profile_from_obj};
use crate::error::to_py;
use crate::types::{
    PyFeedback, PyFeedbackStats, PyMessage, PyModelSelection, PyRequestMetadata, PyRetrievedItem,
    PyRouteContext, PyRouteHint, PyRouteRequest, PyRoutingKey, PyStateQuery, PyStateView,
    PyToolCall,
};

struct PyKvCoordinator {
    cb: Py<PyAny>,
}

impl KvCacheCoordinator for PyKvCoordinator {
    fn on_switch(&self, from: &str, to: &str) {
        Python::with_gil(|py| {
            let _ = self.cb.bind(py).call1((from, to));
        });
    }
}

#[pyclass(name = "Router")]
pub struct PyRouter {
    inner: Router,
}

#[pymethods]
impl PyRouter {
    /// `from_config(path | dict, *, state=None)`。dict 便于配置中心注入。
    #[staticmethod]
    #[pyo3(signature = (config, *, state=None))] // config 是必填参数；state 是可选参数；* 表示 state 必须使用关键字传递；
                                                 // PyAny表示 config 是一个任意 Python 对象，在python中支持配置文件和字典两种形式；
    fn from_config(config: Bound<'_, PyAny>, state: Option<Bound<'_, PyAny>>) -> PyResult<Self> {
        assemble(profile_from_obj(&config)?, state)
    }

    #[staticmethod]
    fn from_toml(text: &str) -> PyResult<Self> {
        assemble(
            openjiuwen_runtime::RouterProfile::from_toml(text).map_err(to_py)?,
            None,
        )
    }

    /// 同步决策。接受 RouteRequest 或 dict；hint 可为 RouteHint / str / dict / None。
    /// 跨边界返回 ModelSelection（Decision 的投影）。
    #[pyo3(signature = (request, hint=None))]
    fn route(
        &self,
        request: Bound<'_, PyAny>,
        hint: Option<Bound<'_, PyAny>>,
    ) -> PyResult<PyModelSelection> {
        let req = extract_request(&request)?;
        let hint = extract_hint(hint.as_ref())?;
        let d = self.inner.route(&req, &hint).map_err(to_py)?;
        Ok(PyModelSelection::from_decision(&d))
    }

    /// 上报反馈。走与 Rust 侧 `Router::try_report` 相同的校验入口；
    /// 非法反馈抛出 `ValueError`，不会写入 state。
    fn report(&self, feedback: Bound<'_, PyAny>) -> PyResult<()> {
        let fb = convert::extract_feedback(&feedback)?;
        self.inner.try_report(fb).map_err(convert::feedback_error)?;
        Ok(())
    }

    fn algorithm_name(&self) -> &str {
        self.inner.algorithm_name()
    }

    fn with_kv_coordinator(&mut self, cb: Bound<'_, PyAny>) {
        self.inner
            .set_kv_coordinator(Box::new(PyKvCoordinator { cb: cb.unbind() }));
    }
}

fn assemble(
    profile: openjiuwen_runtime::RouterProfile,
    state: Option<Bound<'_, PyAny>>,
) -> PyResult<PyRouter> {
    let algorithm = match adapter::lookup(&profile.algorithm) {
        Some(algo) => algo,
        None => registry::create_algorithm(&profile.algorithm).map_err(to_py)?,
    };
    let state: Arc<dyn StateProvider> = match state {
        Some(obj) => extract_state(&obj)?,
        None => match state_adapter::lookup(&profile.state.backend) {
            Some(s) => s,
            None => Router::state_from_profile(&profile).map_err(to_py)?,
        },
    };
    Ok(PyRouter {
        inner: Router::from_parts(algorithm, state, TargetSet::new(profile.targets.models)),
    })
}

/// 任意实现 `snapshot` / `report` 的 Python `StateProvider`。
fn extract_state(obj: &Bound<'_, PyAny>) -> PyResult<Arc<dyn StateProvider>> {
    state_adapter::adapter_from_obj(obj.clone())
}

#[pymodule]
fn _openjiuwen(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyRouter>()?;
    m.add_class::<PyModelSelection>()?;
    m.add_class::<PyRouteRequest>()?;
    m.add_class::<PyRouteHint>()?;
    m.add_class::<PyStateQuery>()?;
    m.add_class::<PyRetrievedItem>()?;
    m.add_class::<PyMessage>()?;
    m.add_class::<PyToolCall>()?;
    m.add_class::<PyRequestMetadata>()?;
    m.add_class::<PyRoutingKey>()?;
    m.add_class::<PyFeedback>()?;
    m.add_class::<types::PyCallFeedback>()?;
    m.add_class::<types::PyExtension>()?;
    m.add_class::<PyStateView>()?;
    m.add_class::<PyFeedbackStats>()?;
    m.add_class::<PyRouteContext>()?;
    m.add_function(wrap_pyfunction!(adapter::register_algorithm, m)?)?;
    m.add_function(wrap_pyfunction!(state_adapter::register_state, m)?)?;
    m.add("Decision", m.getattr("ModelSelection")?)?;
    m.add("OK", "ok")?;
    m.add("OVERFLOW", "overflow")?;
    m.add("UNAVAILABLE", "unavailable")?;
    m.add("REJECTED", "rejected")?;
    Ok(())
}
