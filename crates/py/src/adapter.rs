//! Python 算法 → Rust [`AlgorithmProvider`] 反向绑定。

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};

use pyo3::prelude::*;
use pyo3::types::PyAnyMethods;

use openjiuwen_algorithms::{AlgorithmProvider, RouteContext};
use openjiuwen_protocol::{Decision, RouteRequest, RouterError};

use crate::convert;

fn py_algorithms() -> &'static Mutex<HashMap<String, Py<PyAny>>> {
    static PY_ALGORITHMS: OnceLock<Mutex<HashMap<String, Py<PyAny>>>> = OnceLock::new();
    PY_ALGORITHMS.get_or_init(|| Mutex::new(HashMap::new()))
}

pub struct PyAlgorithmAdapter {
    name: String,
    obj: Py<PyAny>,
}

impl PyAlgorithmAdapter {
    pub fn new(name: String, obj: Py<PyAny>) -> Self {
        Self { name, obj }
    }
}

impl AlgorithmProvider for PyAlgorithmAdapter {
    fn name(&self) -> &str {
        &self.name
    }

    fn decide(&self, request: &RouteRequest, ctx: &RouteContext) -> Result<Decision, RouterError> {
        Python::with_gil(|py| {
            let py_req = convert::py_route_request(py, request)
                .map_err(|e| RouterError::Algorithm(format!("encode request: {e}")))?;
            let py_ctx = convert::py_route_context(py, ctx)
                .map_err(|e| RouterError::Algorithm(format!("encode context: {e}")))?;
            let result = self
                .obj
                .bind(py)
                .call_method1("decide", (py_req, py_ctx))
                .map_err(|e| {
                    let msg = e.to_string();
                    if msg.contains("no available target") {
                        RouterError::NoTarget
                    } else {
                        RouterError::Algorithm(msg)
                    }
                })?;
            convert::extract_decision(&result).map_err(|e| RouterError::Algorithm(e.to_string()))
        })
    }
}

pub fn lookup(name: &str) -> Option<Box<dyn AlgorithmProvider>> {
    let obj = {
        let map = py_algorithms().lock().unwrap_or_else(|e| e.into_inner());
        map.get(name)
            .map(|obj| Python::with_gil(|py| obj.clone_ref(py)))
    }?;
    Some(Box::new(PyAlgorithmAdapter::new(name.to_string(), obj)))
}

#[pyfunction(name = "_register_algorithm")]
pub fn register_algorithm(obj: Bound<'_, PyAny>) -> PyResult<String> {
    let instance = convert::as_instance(&obj)?;
    if !instance.hasattr("decide")? {
        return Err(pyo3::exceptions::PyTypeError::new_err(
            "algorithm must implement decide(request, ctx)",
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
            "algorithm.name must be a non-empty stable name",
        ));
    }
    let mut map = py_algorithms().lock().unwrap_or_else(|e| e.into_inner());
    map.insert(name.clone(), instance.unbind());
    Ok(name)
}
