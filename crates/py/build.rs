/// Whether this build is producing the Python extension.
///
/// Cargo exports an enabled feature as `CARGO_FEATURE_<NAME>=1`. The same crate
/// is also built as an rlib, and that build must not receive cdylib link flags.
fn linking_python_extension() -> bool {
    matches!(
        std::env::var("CARGO_FEATURE_EXTENSION_MODULE").as_deref(),
        Ok("1")
    )
}

fn main() {
    if linking_python_extension() {
        pyo3_build_config::add_extension_module_link_args();
    }
}
