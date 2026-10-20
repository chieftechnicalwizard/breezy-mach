use axum::{
    extract::{Query, State},
    http::StatusCode,
    response::{Html, IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tower_http::cors::CorsLayer;

use crate::ble::ThermostatManager;
use crate::protocol::ThermostatState;

const HTML_DASHBOARD: &str = include_str!("../dashboard.html");

#[derive(Clone)]
pub struct AppState {
    pub manager: Arc<ThermostatManager>,
}

pub fn create_router(manager: Arc<ThermostatManager>) -> Router {
    let state = AppState { manager };

    Router::new()
        .route("/", get(handle_index))
        .route("/api/status", get(handle_api_status))
        .route("/api/set", post(handle_api_set))
        .route("/api/scan", get(handle_api_scan))
        .route("/api/pair", post(handle_api_pair))
        .route("/api/pair/status", get(handle_api_pair_status))
        .route("/api/config", get(handle_api_config))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

async fn handle_index() -> Html<&'static str> {
    Html(HTML_DASHBOARD)
}

#[derive(Deserialize)]
struct StatusQuery {
    fresh: Option<bool>,
}

#[derive(Serialize)]
struct StatusResponse {
    success: bool,
    state: Option<ThermostatState>,
    error: Option<String>,
}

async fn handle_api_status(
    State(state): State<AppState>,
    Query(query): Query<StatusQuery>,
) -> impl IntoResponse {
    let fresh = query.fresh.unwrap_or(false);
    match state.manager.get_state(fresh).await {
        Ok(s) => Json(StatusResponse {
            success: true,
            state: Some(s),
            error: None,
        })
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(StatusResponse {
                success: false,
                state: None,
                error: Some(e.to_string()),
            }),
        )
            .into_response(),
    }
}

#[derive(Deserialize)]
struct SetRequest {
    temp: Option<u8>,
    mode: Option<String>,
    zone: Option<String>,
}

async fn handle_api_set(
    State(state): State<AppState>,
    Json(payload): Json<SetRequest>,
) -> impl IntoResponse {
    match state
        .manager
        .apply_settings(payload.temp, payload.mode.as_deref(), payload.zone.as_deref())
        .await
    {
        Ok(s) => Json(StatusResponse {
            success: true,
            state: Some(s),
            error: None,
        })
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(StatusResponse {
                success: false,
                state: None,
                error: Some(e.to_string()),
            }),
        )
            .into_response(),
    }
}

async fn handle_api_scan(State(state): State<AppState>) -> impl IntoResponse {
    match state.manager.scan_devices().await {
        Ok(devs) => Json(serde_json::json!({
            "success": true,
            "devices": devs,
        }))
        .into_response(),
        Err(e) => (
            StatusCode::INTERNAL_SERVER_ERROR,
            Json(serde_json::json!({
                "success": false,
                "error": e.to_string(),
            })),
        )
            .into_response(),
    }
}

#[derive(Deserialize)]
struct PairRequest {
    pin: Option<String>,
    #[allow(dead_code)]
    wait: Option<u64>,
}

async fn handle_api_pair(
    State(state): State<AppState>,
    Json(payload): Json<PairRequest>,
) -> impl IntoResponse {
    let (started, msg) = state.manager.start_pairing(payload.pin).await;
    Json(serde_json::json!({
        "success": started,
        "message": msg,
    }))
}

async fn handle_api_pair_status(State(state): State<AppState>) -> impl IntoResponse {
    let (active, status, message) = state.manager.get_pairing_status().await;
    Json(serde_json::json!({
        "success": true,
        "active": active,
        "status": status,
        "message": message,
    }))
}

async fn handle_api_config(State(state): State<AppState>) -> impl IntoResponse {
    let cfg = state.manager.get_config().await;
    Json(serde_json::json!({
        "success": true,
        "config": cfg,
    }))
}
