//! Python 状态 → Rust [`StateProvider`] 反向绑定。

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;

use openjiuwen_protocol::{
    Feedback, RoutingKey, StateQuery, StateQueryError, StateSnapshot, StateView,
};
use openjiuwen_state::{CasConflict, StateProvider};

use crate::convert;
use crate::types::{PyFeedback, PyRoutingKey, PyStateQuery};

fn py_states() -> &'static Mutex<HashMap<String, Py<PyAny>>> {
    static PY_STATES: OnceLock<Mutex<HashMap<String, Py<PyAny>>>> = OnceLock::new();
    PY_STATES.get_or_init(|| Mutex::new(HashMap::new()))
}

pub struct PyStateAdapter {
    obj: Py<PyAny>,
}

impl PyStateAdapter {
    pub fn new(obj: Py<PyAny>) -> Self {
        Self { obj }
    }
}

impl StateProvider for PyStateAdapter {
    fn snapshot(&self, key: &RoutingKey) -> StateView {
        Python::with_gil(|py| {
            let py_key = match Bound::new(py, PyRoutingKey::from(key)) {
                Ok(k) => k,
                Err(_) => return StateView::empty(),
            };
            match self.obj.bind(py).call_method1("snapshot", (py_key,)) {
                Ok(result) => {
                    convert::extract_state_view(&result).unwrap_or_else(|_| StateView::empty())
                }
                Err(_) => StateView::empty(),
            }
        })
    }

    /// Python 侧实现了 `query` 就走它，否则回退 `snapshot`。
    ///
    /// 旧版 Python StateProvider 无需改动：没有 `query` 属性时行为与之前完全一致。
    fn query(
        &self,
        key: &RoutingKey,
        query: &StateQuery,
    ) -> Result<StateSnapshot, StateQueryError> {
        Python::with_gil(|py| {
            let obj = self.obj.bind(py);
            let has_query = obj.hasattr("query").unwrap_or(false);
            if !has_query {
                return Ok(StateSnapshot::from_view(self.snapshot(key)));
            }
            let py_key = Bound::new(py, PyRoutingKey::from(key))
                .map_err(|e| StateQueryError::Backend(e.to_string()))?;
            let py_query = Bound::new(py, PyStateQuery::from_native(query))
                .map_err(|e| StateQueryError::Backend(e.to_string()))?;
            match obj.call_method1("query", (py_key, py_query)) {
                Ok(result) => Ok(convert::extract_state_snapshot(&result)
                    .unwrap_or_else(|_| StateSnapshot::from_view(self.snapshot(key)))),
                Err(e) => Err(StateQueryError::Backend(e.to_string())),
            }
        })
    }

    fn report(&self, feedback: Feedback) {
        Python::with_gil(|py| {
            let py_fb = match Bound::new(py, PyFeedback::from_native(&feedback)) {
                Ok(fb) => fb,
                Err(_) => return,
            };
            let _ = self.obj.bind(py).call_method1("report", (py_fb,));
        });
    }

    fn publish(&self, slot: &str, artifact: &[u8], ver: u64) -> Result<(), CasConflict> {
        Python::with_gil(|py| {
            let obj = self.obj.bind(py);
            if !obj.hasattr("publish").unwrap_or(false) {
                return Ok(());
            }
            obj.call_method1("publish", (slot, artifact.to_vec(), ver))
                .map(|_| ())
                .map_err(|_| CasConflict {
                    slot: slot.to_string(),
                    expected: ver,
                    actual: 0,
                })
        })
    }
}

pub fn lookup(name: &str) -> Option<Arc<dyn StateProvider>> {
    let obj = {
        let map = py_states().lock().unwrap_or_else(|e| e.into_inner());
        map.get(name)
            .map(|obj| Python::with_gil(|py| obj.clone_ref(py)))
    }?;
    Some(Arc::new(PyStateAdapter::new(obj)))
}

#[pyfunction]
pub fn register_state(obj: Bound<'_, PyAny>) -> PyResult<String> {
    let instance = convert::as_instance(&obj)?;
    if !instance.hasattr("snapshot")? || !instance.hasattr("report")? {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "state must implement snapshot(key) and report(feedback)",
        ));
    }
    let name: String = if instance.hasattr("name")? {
        let attr = instance.getattr("name")?;
        if attr.is_callable() {
            attr.call0()?.extract()?
        } else {
            attr.extract()?
        }
    } else {
        "unnamed".into()
    };
    if name.is_empty() || name == "unnamed" {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "state.name must be a non-empty stable name",
        ));
    }
    let mut map = py_states().lock().unwrap_or_else(|e| e.into_inner());
    map.insert(name.clone(), instance.unbind());
    Ok(name)
}

pub fn adapter_from_obj(obj: Bound<'_, PyAny>) -> PyResult<Arc<dyn StateProvider>> {
    let instance = convert::as_instance(&obj)?;
    if !instance.hasattr("snapshot")? || !instance.hasattr("report")? {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "state must implement snapshot(key) and report(feedback)",
        ));
    }
    Ok(Arc::new(PyStateAdapter::new(instance.unbind())))
}
