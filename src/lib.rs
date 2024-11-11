mod watch;

use pyo3::prelude::*;

#[pymodule]
mod _rust {
    use super::*;
    
    #[pymodule]
    mod watch {
        use super::*;
        
        #[pymodule_export]
        use crate::watch::Watch;
        
        #[pymodule_export]
        use crate::watch::WatchEvent;
        
        #[pymodule_export]
        use crate::watch::WatchEventType;
        
        /// I love PyO3
        /// https://github.com/PyO3/pyo3/issues/759
        #[pymodule_init]
        fn init(m: &Bound<'_, PyModule>) -> PyResult<()> {
            Python::with_gil(|py| {
                py.import_bound("sys")?
                    .getattr("modules")?
                    .set_item("wakapi_anyide._rust.watch", m)?;
                m.setattr("__name__", "wakapi_anyide._rust.watch")
            })
        }
    }
}