//! 决策循环：snapshot/query → 装配 RouteContext → decide → Decision。

use openjiuwen_algorithms::{AlgorithmProvider, RouteContext};
use openjiuwen_protocol::{Decision, StateView, RouteHint, RouteRequest, RouterError, TargetSet};
use openjiuwen_state::StateProvider;

/// 读取状态：有检索意图走 `query`，否则走基础 `snapshot`。
///
/// 检索失败一律降级为普通快照，绝不让检索能力影响路由可用性。
///
/// `query` 收到的 `StateQuery` 带上本次 `route` 的 `route_id`：宿主填的值被覆盖，
/// 保证 state 看到的 id 与随后写进 `Decision` 的是同一个。
fn read_state(
    state: &dyn StateProvider,
    key: &openjiuwen_protocol::RoutingKey,
    hint: &RouteHint,
    route_id: &str,
) -> (StateView, Vec<openjiuwen_protocol::RetrievedItem>) {
    let Some(query) = hint.state_query.as_ref() else {
        return (state.snapshot(key), Vec::new());
    };
    if query.is_empty() || query.validate().is_err() {
        return (state.snapshot(key), Vec::new());
    }
    let state_query: openjiuwen_protocol::StateQuery = query.clone().with_route_id(route_id);
    match state.query(key, &state_query) {
        Ok(snapshot) if snapshot.validate().is_ok() => (snapshot.view, snapshot.retrieved),
        Ok(snapshot) => (snapshot.view, Vec::new()),
        Err(_) => (state.snapshot(key), Vec::new()),
    }
}

/// 驱动一次纯函数决策。重试时由宿主经 `req.exclusions` 排除已败目标。
///
/// `route_id` 由调用方（`Router::route`）在进入前生成，这里只把它交给 state 的
/// `query`；写回 `Decision` 仍由调用方完成，算法全程不接触它。
pub fn run(
    algorithm: &dyn AlgorithmProvider,    // 算法实例       
    state: &dyn StateProvider,    // 状态实例
    req: &RouteRequest,    // 路由请求
    hint: &RouteHint,    // 路由提示
    catalog: &TargetSet,    // 目标集合
    seed: u64,    // 随机种子
    route_id: &str,    // 本次 route 的关联 id，仅透传给 state.query
) -> Result<Decision, RouterError> {    // 返回的是 Result<Decision, RouterError> 类型。
    let (view, retrieved) = read_state(state, &req.routing_key(), hint, route_id);
    let mut exclusions = req.exclusions.clone();
    // 排除列表扩展。
    exclusions.extend(view.exclusions.iter().cloned());
    // 目标集合过滤。
    let targets = catalog.without(&exclusions);

    let ctx: RouteContext = RouteContext {
        targets,
        view,
        retrieved,
        seed,
    };
    algorithm.decide(req, &ctx)
}
