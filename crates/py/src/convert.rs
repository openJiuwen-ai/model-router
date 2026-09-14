//! Python 对象 → 协议类型。

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySequence, PyString, PyType};

use openjiuwen_algorithms::RouteContext;
use openjiuwen_protocol::{
    Extension, Message, Outcome, RequestMetadata, RetrievedItem, RouteHint, RouteRequest,
    RouterError, RoutingKey, StateQuery, StateSnapshot, StateView, MAX_EXTENSIONS,
    QUERY_MAX_RETRIEVED,
};
use openjiuwen_runtime::config::{RouterProfile, StateConfig, TargetsConfig};

use crate::types::{
    PyExtension, PyMessage, PyModelSelection, PyRequestMetadata, PyRetrievedItem, PyRouteContext,
    PyRouteHint, PyRouteRequest, PyRoutingKey, PyStateQuery, PyStateView,
};

pub fn feedback_error(error: openjiuwen_protocol::FeedbackError) -> PyErr {
    PyValueError::new_err(error.to_string())
}

pub fn extract_json(obj: &Bound<'_, PyAny>) -> PyResult<openjiuwen_protocol::Value> {
    /// 与 `openjiuwen_protocol::Value::validate_inner` 相同的计费与检查顺序：
    /// 先加本节点开销并检查字节上限，再检查深度，再累加计数。
    /// 字符串按 UTF-8 字节计，其余节点计 8 字节；不能先统一 +8 再对字符串回退。
    fn charge(bytes: &mut usize, count: &mut usize, depth: usize, cost: usize) -> PyResult<()> {
        use openjiuwen_protocol::feedback::{VALUE_MAX_BYTES, VALUE_MAX_DEPTH, VALUE_MAX_ELEMENTS};
        *bytes += cost;
        if *bytes > VALUE_MAX_BYTES {
            return Err(feedback_error(
                openjiuwen_protocol::FeedbackError::PayloadTooLarge,
            ));
        }
        if depth > VALUE_MAX_DEPTH {
            return Err(feedback_error(
                openjiuwen_protocol::FeedbackError::NestedTooDeep,
            ));
        }
        *count += 1;
        if *count > VALUE_MAX_ELEMENTS {
            return Err(feedback_error(
                openjiuwen_protocol::FeedbackError::PayloadTooLarge,
            ));
        }
        Ok(())
    }

    fn walk(
        obj: &Bound<'_, PyAny>,
        depth: usize,
        count: &mut usize,
        bytes: &mut usize,
    ) -> PyResult<openjiuwen_protocol::Value> {
        use openjiuwen_protocol::Value;
        use pyo3::types::{PyBool, PyFloat, PyInt};
        if obj.is_none() {
            charge(bytes, count, depth, 8)?;
            Ok(Value::Null)
        } else if obj.is_instance_of::<PyBool>() {
            charge(bytes, count, depth, 8)?;
            Ok(Value::Bool(obj.extract()?))
        } else if obj.is_instance_of::<PyInt>() {
            charge(bytes, count, depth, 8)?;
            Ok(Value::Integer(obj.extract::<i64>().map_err(|_| {
                feedback_error(openjiuwen_protocol::FeedbackError::IntegerOverflow)
            })?))
        } else if obj.is_instance_of::<PyFloat>() {
            charge(bytes, count, depth, 8)?;
            let f: f64 = obj.extract()?;
            if !f.is_finite() {
                return Err(feedback_error(
                    openjiuwen_protocol::FeedbackError::NonFiniteNumber,
                ));
            }
            Ok(Value::Float(f))
        } else if obj.is_instance_of::<PyString>() {
            let s: String = obj.extract()?;
            charge(bytes, count, depth, s.len())?;
            Ok(Value::String(s))
        } else if let Ok(list) = obj.downcast::<PyList>() {
            charge(bytes, count, depth, 8)?;
            let mut items = Vec::with_capacity(list.len());
            for item in list.iter() {
                items.push(walk(&item, depth + 1, count, bytes)?);
            }
            Ok(Value::Array(items))
        } else if let Ok(dict) = obj.downcast::<PyDict>() {
            charge(bytes, count, depth, 8)?;
            let mut pairs = Vec::with_capacity(dict.len());
            for (k, v) in dict.iter() {
                let key: String = k.extract()?;
                *bytes += key.len();
                if *bytes > VALUE_MAX_BYTES {
                    return Err(feedback_error(
                        openjiuwen_protocol::FeedbackError::PayloadTooLarge,
                    ));
                }
                pairs.push((key, walk(&v, depth + 1, count, bytes)?));
            }
            Ok(Value::Object(pairs))
        } else {
            Err(PyValueError::new_err("extension data must be JSON: null, bool, i64, finite float, str, list, or string-keyed dict"))
        }
    }
    use openjiuwen_protocol::feedback::VALUE_MAX_BYTES;
    let value = walk(obj, 1, &mut 0, &mut 0)?;
    value.validate().map_err(feedback_error)?;
    Ok(value)
}

pub fn json_to_py<'py>(
    py: Python<'py>,
    value: &openjiuwen_protocol::Value,
) -> PyResult<Bound<'py, PyAny>> {
    use openjiuwen_protocol::Value;
    Ok(match value {
        Value::Null => py.None().into_bound(py),
        Value::Bool(v) => v.into_pyobject(py)?.to_owned().into_any(),
        Value::Integer(v) => v.into_pyobject(py)?.into_any(),
        Value::Float(v) => v.into_pyobject(py)?.into_any(),
        Value::String(v) => v.into_pyobject(py)?.into_any(),
        Value::Array(items) => {
            let list = PyList::empty(py);
            for v in items {
                list.append(json_to_py(py, v)?)?;
            }
            list.into_any()
        }
        Value::Object(pairs) => {
            let dict = PyDict::new(py);
            for (k, v) in pairs {
                dict.set_item(k, json_to_py(py, v)?)?;
            }
            dict.into_any()
        }
    })
}

fn required<'py>(dict: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    dict.get_item(key)?
        .ok_or_else(|| PyValueError::new_err(format!("missing field: {key}")))
}

pub fn unsigned_integer(value: &Bound<'_, PyAny>) -> PyResult<u64> {
    if value.is_instance_of::<pyo3::types::PyBool>()
        || !value.is_instance_of::<pyo3::types::PyInt>()
    {
        return Err(PyValueError::new_err("expected unsigned integer"));
    }
    value.extract()
}

fn strict_u64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u64>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(unsigned_integer(&v)?)),
        _ => Ok(None),
    }
}

pub fn extract_feedback(obj: &Bound<'_, PyAny>) -> PyResult<openjiuwen_protocol::Feedback> {
    use crate::types::{PyCallFeedback, PyFeedback};
    use openjiuwen_protocol::{feedback::FEEDBACK_VERSION, CallFeedback, Feedback};
    if let Ok(fb) = obj.extract::<PyRef<PyFeedback>>() {
        return fb.native();
    }
    let dict = obj
        .downcast::<PyDict>()
        .map_err(|_| PyValueError::new_err("feedback must be Feedback or dict"))?;
    for (key, _) in dict.iter() {
        let key: String = key.extract()?;
        if ![
            "version",
            "event_id",
            "route_id",
            "key",
            "session_id",
            "agent_id",
            "selected_model_id",
            "observed_at_ms",
            "call",
            "extensions",
            "outcome",
            "latency_ms",
            "cache_valid",
        ]
        .contains(&key.as_str())
        {
            return Err(PyValueError::new_err(format!(
                "unknown feedback field: {key}"
            )));
        }
    }
    let key = match dict.get_item("key")? {
        Some(k) if !k.is_none() => extract_routing_key(&k)?,
        _ => RoutingKey {
            session_id: opt_dict_str(dict, "session_id")?.unwrap_or_default(),
            agent_id: opt_dict_str(dict, "agent_id")?.unwrap_or_default(),
        },
    };
    fn call_dict(dict: &Bound<'_, PyDict>, legacy: bool) -> PyResult<CallFeedback> {
        let outcome = opt_dict_str(dict, "outcome")?.unwrap_or_else(|| "ok".into());
        let latency = strict_u64(dict, "latency_ms")?;
        let cache_valid = match dict.get_item("cache_valid")? {
            Some(v) if !v.is_none() => {
                if !v.is_instance_of::<pyo3::types::PyBool>() {
                    return Err(PyValueError::new_err("cache_valid must be bool or None"));
                }
                Some(v.extract()?)
            }
            _ => None,
        };
        Ok(CallFeedback {
            outcome: parse_outcome(&outcome)?,
            latency_ms: if legacy {
                Some(latency.unwrap_or(0))
            } else {
                latency
            },
            cache_valid,
        })
    }
    let call = match dict.get_item("call")? {
        Some(raw) => {
            // 混用判定统一为“字段存在且非 None”。显式 None 视为未提供，
            // 与 typed 构造器的 Option 哨兵保持一致。
            for key in ["outcome", "latency_ms", "cache_valid"] {
                match dict.get_item(key)? {
                    Some(v) if !v.is_none() => {
                        return Err(PyValueError::new_err(
                            "call cannot be combined with legacy call fields",
                        ))
                    }
                    _ => {}
                }
            }
            if raw.is_none() {
                None
            } else if let Ok(c) = raw.extract::<PyRef<PyCallFeedback>>() {
                Some(CallFeedback {
                    outcome: parse_outcome(&c.outcome)?,
                    latency_ms: c.latency_ms,
                    cache_valid: c.cache_valid,
                })
            } else {
                let d = raw.downcast::<PyDict>()?;
                required(d, "outcome")?.extract::<String>()?;
                for (k, _) in d.iter() {
                    if !["outcome", "latency_ms", "cache_valid"]
                        .contains(&k.extract::<String>()?.as_str())
                    {
                        return Err(PyValueError::new_err("unknown call field"));
                    }
                }
                Some(call_dict(d, false)?)
            }
        }
        None => Some(call_dict(dict, true)?),
    };
    let extensions = match dict.get_item("extensions")? {
        Some(raw) if !raw.is_none() => extract_extensions(&raw)?,
        _ => Vec::new(),
    };
    let version = strict_u64(dict, "version")?.unwrap_or(FEEDBACK_VERSION.into());
    let fb = Feedback {
        version: u32::try_from(version)
            .map_err(|_| PyValueError::new_err("version out of u32 range"))?,
        event_id: opt_dict_str(dict, "event_id")?,
        route_id: opt_dict_str(dict, "route_id")?,
        key,
        selected_model_id: required(dict, "selected_model_id")?.extract()?,
        observed_at_ms: strict_u64(dict, "observed_at_ms")?,
        call,
        extensions,
    };
    fb.validate().map_err(feedback_error)?;
    Ok(fb)
}

pub fn feedback_to_dict<'py>(
    py: Python<'py>,
    fb: &openjiuwen_protocol::Feedback,
) -> PyResult<Bound<'py, PyDict>> {
    fb.validate().map_err(feedback_error)?;
    let d = PyDict::new(py);
    d.set_item("version", fb.version)?;
    d.set_item("event_id", &fb.event_id)?;
    d.set_item("route_id", &fb.route_id)?;
    let key = PyDict::new(py);
    key.set_item("session_id", &fb.key.session_id)?;
    key.set_item("agent_id", &fb.key.agent_id)?;
    d.set_item("key", key)?;
    d.set_item("selected_model_id", &fb.selected_model_id)?;
    d.set_item("observed_at_ms", fb.observed_at_ms)?;
    if let Some(c) = &fb.call {
        let call = PyDict::new(py);
        call.set_item("outcome", crate::types::outcome_name(c.outcome))?;
        call.set_item("latency_ms", c.latency_ms)?;
        call.set_item("cache_valid", c.cache_valid)?;
        d.set_item("call", call)?;
    } else {
        d.set_item("call", py.None())?;
    }
    let extensions = PyList::empty(py);
    for e in &fb.extensions {
        let ext = PyDict::new(py);
        ext.set_item("schema", &e.schema)?;
        ext.set_item("version", &e.version)?;
        ext.set_item("data", json_to_py(py, &e.data)?)?;
        extensions.append(ext)?;
    }
    d.set_item("extensions", extensions)?;
    Ok(d)
}

pub fn parse_outcome(raw: &str) -> PyResult<Outcome> {
    match raw.trim().to_ascii_lowercase().as_str() {
        "ok" => Ok(Outcome::Ok),
        "overflow" => Ok(Outcome::Overflow),
        "unavailable" => Ok(Outcome::Unavailable),
        "rejected" => Ok(Outcome::Rejected),
        other => Err(PyValueError::new_err(format!(
            "unknown outcome: {other} (ok|overflow|unavailable|rejected)"
        ))),
    }
}

pub fn extract_routing_key(obj: &Bound<'_, PyAny>) -> PyResult<RoutingKey> {
    if obj.is_none() {
        return Ok(RoutingKey::default());
    }
    if let Ok(key) = obj.extract::<PyRef<PyRoutingKey>>() {
        return Ok(key.native());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        return Ok(RoutingKey {
            session_id: opt_dict_str(dict, "session_id")?.unwrap_or_default(),
            agent_id: opt_dict_str(dict, "agent_id")?.unwrap_or_default(),
        });
    }
    Err(PyValueError::new_err(
        "routing key must be RoutingKey or dict with session_id/agent_id",
    ))
}

pub fn selection_model_id(obj: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(sel) = obj.extract::<PyRef<PyModelSelection>>() {
        return Ok(sel.selected_model_id.clone());
    }
    if obj.hasattr("selected_model_id")? {
        return obj.getattr("selected_model_id")?.extract();
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        if let Some(v) = dict.get_item("selected_model_id")? {
            return v.extract();
        }
        if let Some(v) = dict.get_item("target")? {
            return v.extract();
        }
    }
    Err(PyValueError::new_err(
        "decision must expose selected_model_id (ModelSelection, dict, or attribute)",
    ))
}

pub fn extract_message(obj: &Bound<'_, PyAny>) -> PyResult<Message> {
    if let Ok(msg) = obj.extract::<PyRef<PyMessage>>() {
        return Ok(Message::from(&*msg));
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        return Ok(Message {
            role: dict_str(dict, "role")?.unwrap_or_default(),
            content: dict_str(dict, "content")?.unwrap_or_default(),
        });
    }
    if let Ok(seq) = obj.downcast::<PySequence>() {
        if seq.len()? >= 2 {
            return Ok(Message {
                role: seq.get_item(0)?.extract()?,
                content: seq.get_item(1)?.extract()?,
            });
        }
    }
    Err(PyValueError::new_err(
        "message must be Message, dict{role,content}, or (role, content)",
    ))
}

pub fn extract_metadata(obj: &Bound<'_, PyAny>) -> PyResult<RequestMetadata> {
    if obj.is_none() {
        return Ok(RequestMetadata::default());
    }
    if let Ok(meta) = obj.extract::<PyRef<PyRequestMetadata>>() {
        return Ok(meta.native());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        return Ok(RequestMetadata {
            session_id: opt_dict_str(dict, "session_id")?,
            agent_id: opt_dict_str(dict, "agent_id")?,
        });
    }
    Err(PyValueError::new_err(
        "metadata must be RequestMetadata or dict",
    ))
}

pub fn extract_request(obj: &Bound<'_, PyAny>) -> PyResult<RouteRequest> {
    if let Ok(req) = obj.extract::<PyRef<PyRouteRequest>>() {
        return Ok(req.native());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let mut messages = Vec::new();
        if let Some(raw) = dict.get_item("messages")? {
            if !raw.is_none() {
                for item in raw.try_iter()? {
                    messages.push(extract_message(&item?)?);
                }
            }
        }
        let metadata = match dict.get_item("metadata")? {
            Some(m) if !m.is_none() => extract_metadata(&m)?,
            _ => RequestMetadata {
                session_id: opt_dict_str(dict, "session_id")?,
                agent_id: opt_dict_str(dict, "agent_id")?,
            },
        };
        let exclusions = match dict.get_item("exclusions")? {
            Some(v) if !v.is_none() => v.extract()?,
            _ => Vec::new(),
        };
        return Ok(RouteRequest {
            messages,
            metadata,
            exclusions,
        });
    }
    Err(PyValueError::new_err(
        "request must be RouteRequest or dict with messages/metadata/exclusions",
    ))
}

pub fn extract_hint(obj: Option<&Bound<'_, PyAny>>) -> PyResult<RouteHint> {
    let Some(obj) = obj else {
        return Ok(RouteHint::default());
    };
    if obj.is_none() {
        return Ok(RouteHint::default());
    }
    if let Ok(hint) = obj.extract::<PyRef<PyRouteHint>>() {
        return Ok(hint.native());
    }
    if obj.downcast::<PyString>().is_ok() {
        return Ok(RouteHint {
            cache_affinity: Some(obj.extract()?),
            state_query: None,
        });
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let state_query = match dict.get_item("state_query")? {
            Some(v) if !v.is_none() => Some(extract_state_query(&v)?),
            _ => None,
        };
        return Ok(RouteHint {
            cache_affinity: opt_dict_str(dict, "cache_affinity")?,
            state_query,
        });
    }
    Err(PyValueError::new_err(
        "hint must be RouteHint, str, dict, or None",
    ))
}

/// 解析 Python 侧扩展载荷列表：`Extension` 或 dict。
///
/// 与反馈侧共用同一套边界：条数上限 + 未知字段拒绝。
pub fn extract_extensions(raw: &Bound<'_, PyAny>) -> PyResult<Vec<Extension>> {
    let list = raw.downcast::<PyList>()?;
    if list.len() > MAX_EXTENSIONS {
        return Err(PyValueError::new_err("too many extensions"));
    }
    let mut extensions = Vec::new();
    for item in list.iter() {
        let ext = if let Ok(e) = item.extract::<PyRef<PyExtension>>() {
            e.inner.clone()
        } else {
            let e = item.downcast::<PyDict>()?;
            for (k, _) in e.iter() {
                if !["schema", "version", "data"].contains(&k.extract::<String>()?.as_str()) {
                    return Err(PyValueError::new_err("unknown extension field"));
                }
            }
            Extension {
                schema: required(e, "schema")?.extract()?,
                version: required(e, "version")?.extract()?,
                data: extract_json(&required(e, "data")?)?,
            }
        };
        extensions.push(ext);
    }
    Ok(extensions)
}

/// 解析 Python 侧检索意图：`StateQuery` 或 dict。
pub fn extract_state_query(obj: &Bound<'_, PyAny>) -> PyResult<StateQuery> {
    if let Ok(query) = obj.extract::<PyRef<PyStateQuery>>() {
        let native = query.native();
        native
            .validate()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        return Ok(native);
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let text = match dict.get_item("text")? {
            Some(v) if !v.is_none() => Some(v.extract()?),
            _ => None,
        };
        let vector = match dict.get_item("vector")? {
            Some(v) if !v.is_none() => Some(v.extract::<Vec<f32>>()?),
            _ => None,
        };
        let top_k = match dict.get_item("top_k")? {
            Some(v) if !v.is_none() => Some(v.extract::<u32>()?),
            _ => None,
        };
        let extensions = match dict.get_item("extensions")? {
            Some(v) if !v.is_none() => extract_extensions(&v)?,
            _ => Vec::new(),
        };
        // dict 形式不接受 `route_id`：那是 runtime 的关联字段，宿主填了也会被覆盖。
        let query = StateQuery {
            text,
            vector,
            top_k,
            extensions,
            route_id: None,
        };
        query
            .validate()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        return Ok(query);
    }
    Err(PyValueError::new_err(
        "state_query must be StateQuery or dict{text, vector, top_k, extensions}",
    ))
}

pub fn extract_decision(obj: &Bound<'_, PyAny>) -> PyResult<openjiuwen_protocol::Decision> {
    if let Ok(sel) = obj.extract::<PyRef<PyModelSelection>>() {
        return Ok(sel.to_decision());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let selected = dict
            .get_item("selected_model_id")?
            .map(|v| v.extract::<String>())
            .transpose()?
            .or_else(|| {
                dict.get_item("target")
                    .ok()
                    .flatten()
                    .and_then(|v| v.extract::<String>().ok())
            })
            .ok_or_else(|| PyValueError::new_err("decision dict needs selected_model_id"))?;
        let reasoning = dict
            .get_item("reasoning")?
            .map(|v| v.extract::<String>())
            .transpose()?
            .unwrap_or_default();
        let is_answer_call = dict
            .get_item("is_answer_call")?
            .map(|v| v.extract::<bool>())
            .transpose()?
            .unwrap_or(true);
        return Ok(openjiuwen_protocol::Decision {
            route_id: opt_dict_str(dict, "route_id")?,
            selected_model_id: selected,
            reasoning,
            is_answer_call,
        });
    }
    if obj.hasattr("selected_model_id")? {
        return Ok(openjiuwen_protocol::Decision {
            route_id: if obj.hasattr("route_id")? {
                obj.getattr("route_id")?.extract()?
            } else {
                None
            },
            selected_model_id: obj.getattr("selected_model_id")?.extract()?,
            reasoning: if obj.hasattr("reasoning")? {
                obj.getattr("reasoning")?.extract()?
            } else {
                String::new()
            },
            is_answer_call: if obj.hasattr("is_answer_call")? {
                obj.getattr("is_answer_call")?.extract()?
            } else {
                true
            },
        });
    }
    Err(PyValueError::new_err(
        "decide() must return ModelSelection or dict{selected_model_id, reasoning}",
    ))
}

pub fn py_route_context(py: Python<'_>, ctx: &RouteContext) -> PyResult<Py<PyRouteContext>> {
    let retrieved = ctx
        .retrieved
        .iter()
        .map(crate::types::PyRetrievedItem::from_native)
        .collect();
    Bound::new(
        py,
        PyRouteContext {
            targets: ctx.targets.models.clone(),
            view: crate::types::PyStateView::from(&ctx.view),
            retrieved,
            seed: ctx.seed,
        },
    )
    .map(|b| b.unbind())
}

pub fn py_route_request(py: Python<'_>, req: &RouteRequest) -> PyResult<Py<PyRouteRequest>> {
    Bound::new(py, PyRouteRequest::from_native(req)).map(|b| b.unbind())
}

/// 解析 Python `query()` 返回值：`StateSnapshot`、dict{view, retrieved} 或裸视图。
///
/// 兼容旧写法：直接返回 `StateView` / dict{affinity, exclusions} 也能识别，
/// 此时 `retrieved` 视为空。
pub fn extract_state_snapshot(obj: &Bound<'_, PyAny>) -> PyResult<StateSnapshot> {
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let has_retrieved = dict.contains("retrieved")?;
        let has_view = dict.contains("view")?;
        if has_retrieved || has_view {
            let view = match dict.get_item("view")? {
                Some(v) if !v.is_none() => extract_state_view(&v)?,
                _ => StateView::empty(),
            };
            let retrieved = match dict.get_item("retrieved")? {
                Some(v) if !v.is_none() => extract_retrieved_items(&v)?,
                _ => Vec::new(),
            };
            let snapshot = StateSnapshot { view, retrieved };
            snapshot
                .validate()
                .map_err(|e| PyValueError::new_err(e.to_string()))?;
            return Ok(snapshot);
        }
    }
    if obj.hasattr("retrieved")? && obj.hasattr("view")? {
        let view = extract_state_view(&obj.getattr("view")?)?;
        let retrieved = extract_retrieved_items(&obj.getattr("retrieved")?)?;
        let snapshot = StateSnapshot { view, retrieved };
        snapshot
            .validate()
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        return Ok(snapshot);
    }
    Ok(StateSnapshot::from_view(extract_state_view(obj)?))
}

/// 解析检索结果列表：`RetrievedItem` 或 dict{id, score, data}。
pub fn extract_retrieved_items(raw: &Bound<'_, PyAny>) -> PyResult<Vec<RetrievedItem>> {
    let list = raw.downcast::<PyList>()?;
    if list.len() > QUERY_MAX_RETRIEVED {
        return Err(PyValueError::new_err(format!(
            "retrieved items exceed {QUERY_MAX_RETRIEVED}"
        )));
    }
    let mut items = Vec::with_capacity(list.len());
    for item in list.iter() {
        if let Ok(native) = item.extract::<PyRef<PyRetrievedItem>>() {
            items.push(native.native());
            continue;
        }
        let d = item.downcast::<PyDict>()?;
        let id: String = required(d, "id")?.extract()?;
        let score: f64 = required(d, "score")?.extract()?;
        if !score.is_finite() {
            return Err(PyValueError::new_err("retrieved item score is not finite"));
        }
        let data = match d.get_item("data")? {
            Some(v) if !v.is_none() => Some(extract_json(&v)?),
            _ => None,
        };
        items.push(RetrievedItem { id, score, data });
    }
    Ok(items)
}

pub fn extract_state_view(obj: &Bound<'_, PyAny>) -> PyResult<StateView> {
    if obj.is_none() {
        return Ok(StateView::empty());
    }
    if let Ok(view) = obj.extract::<PyRef<PyStateView>>() {
        return Ok(view.native());
    }
    if let Ok(dict) = obj.downcast::<PyDict>() {
        let affinity = opt_dict_str(dict, "affinity")?;
        let exclusions = match dict.get_item("exclusions")? {
            Some(v) if !v.is_none() => v.extract()?,
            _ => Vec::new(),
        };
        let sample_count = match dict.get_item("stats")? {
            Some(stats) if !stats.is_none() => {
                if let Ok(d) = stats.downcast::<PyDict>() {
                    opt_dict_u64(d, "sample_count")?.unwrap_or(0)
                } else if stats.hasattr("sample_count")? {
                    stats.getattr("sample_count")?.extract()?
                } else {
                    0
                }
            }
            _ => 0,
        };
        return Ok(StateView {
            affinity,
            exclusions,
            stats: openjiuwen_protocol::FeedbackStats { sample_count },
        });
    }
    if obj.hasattr("exclusions")? {
        let affinity = if obj.hasattr("affinity")? {
            let v = obj.getattr("affinity")?;
            if v.is_none() {
                None
            } else {
                Some(v.extract()?)
            }
        } else {
            None
        };
        return Ok(StateView {
            affinity,
            exclusions: obj.getattr("exclusions")?.extract()?,
            stats: openjiuwen_protocol::FeedbackStats { sample_count: 0 },
        });
    }
    Err(PyValueError::new_err(
        "snapshot() must return StateView or dict{affinity, exclusions}",
    ))
}

// profile_from_obj 函数用于从 Python 对象中提取路由配置信息，并转换为 Rust 的 RouterProfile 结构体。
pub fn profile_from_obj(obj: &Bound<'_, PyAny>) -> PyResult<RouterProfile> {
    // 如果 obj 是一个字符串，则调用 RouterProfile::from_path 函数从路径中加载配置文件。
    if let Ok(path) = obj.extract::<String>() {
        return RouterProfile::from_path(path).map_err(|e| match e {
            RouterError::Config(msg) => PyValueError::new_err(format!("config: {msg}")),
            other => PyValueError::new_err(other.to_string()),
        });
    }
    // 如果 obj 是一个字典，则调用 profile_from_dict 函数从字典中提取配置信息。
    let dict = obj
        .downcast::<PyDict>()
        .map_err(|_| PyValueError::new_err("from_config expects a path string or dict"))?;
    profile_from_dict(dict)
}

// profile_from_dict 函数用于从字典中提取路由配置信息，并转换为 Rust 的 RouterProfile 结构体。
pub fn profile_from_dict(dict: &Bound<'_, PyDict>) -> PyResult<RouterProfile> {
    // 提取 algorithm 配置项。
    let algorithm = dict
        .get_item("algorithm")?
        .ok_or_else(|| PyValueError::new_err("profile requires algorithm"))?
        .extract::<String>()?;
    // 提取 state 配置项。
    let state = match dict.get_item("state")? {
        Some(s) if !s.is_none() => {
            // 将 state 配置项转换为字典。
            let sd = s
                .downcast::<PyDict>()
                .map_err(|_| PyValueError::new_err("state must be a dict"))?;
            // 将 state 配置项转换为 Rust 的 StateConfig 结构体。
            StateConfig {
                backend: dict_str(sd, "backend")? // 提取 backend 配置项。  
                    .ok_or_else(|| PyValueError::new_err("state.backend is required"))?,
                ttl_secs: opt_dict_u64(sd, "ttl_secs")?, // 提取 ttl_secs 配置项。
                max_entries: opt_dict_usize(sd, "max_entries")?, // 提取 max_entries 配置项。
                endpoint: opt_dict_str(sd, "endpoint")?, // 提取 endpoint 配置项。
                timeout_ms: opt_dict_u64(sd, "timeout_ms")?, // 提取 timeout_ms 配置项。
            }
        }
        _ => {
            // 如果 state 配置项不存在，则返回错误。
            return Err(PyValueError::new_err("profile requires state"));
        }
    };
    // 提取 targets 配置项。
    let models = match dict.get_item("targets")? {
        Some(t) if !t.is_none() => extract_models(&t)?, // 提取 models 配置项。
        _ => Vec::new(),                                // 如果 targets 配置项不存在，则返回空列表。
    };
    // 创建 RouterProfile 结构体。
    Ok(RouterProfile {
        algorithm,
        state,
        targets: TargetsConfig { models }, // 设置 targets 配置项。
        evolving: Vec::new(),              // 设置 evolving 配置项。
    })
}

fn extract_models(obj: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    if let Ok(dict) = obj.downcast::<PyDict>() {
        if let Some(m) = dict.get_item("models")? {
            return m.extract();
        }
        return Ok(Vec::new());
    }
    if obj.downcast::<PyList>().is_ok() {
        return obj.extract();
    }
    Err(PyValueError::new_err(
        "targets must be a list of models or dict{models}",
    ))
}

fn dict_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn opt_dict_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    dict_str(dict, key)
}

fn opt_dict_u64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u64>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

fn opt_dict_usize(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<usize>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract()?)),
        _ => Ok(None),
    }
}

/// 类则 call0 实例化；实例则原样返回。
pub fn as_instance<'py>(obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    if obj.downcast::<PyType>().is_ok() {
        obj.call0()
    } else {
        Ok(obj.clone())
    }
}
