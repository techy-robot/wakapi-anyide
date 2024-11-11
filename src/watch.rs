use notify::event::{ModifyKind, RenameMode};
use notify::{
    recommended_watcher, Event, EventKind, RecommendedWatcher, RecursiveMode,
    Result as NotifyResult, Watcher,
};
use pyo3::exceptions::*;
use pyo3::prelude::*;
use std::cell::RefCell;
use std::path::PathBuf;
use std::sync::mpsc::{channel, Receiver, TryRecvError};
use std::sync::Mutex;
use std::thread;

#[pyclass(eq, eq_int)]
#[derive(PartialEq, Debug, Clone)]
pub enum WatchEventType {
    Create = 0,
    Delete = 1,
    Modify = 2,
}

#[pyclass(frozen, get_all)]
#[derive(Debug)]
#[allow(dead_code)]
pub struct WatchEvent {
    kind: WatchEventType,
    target: PathBuf,
}

impl WatchEvent {
    fn from_event(event: Event) -> Vec<WatchEvent> {
        let mut ret: Vec<WatchEvent> = Vec::new();

        match event.kind {
            EventKind::Other => {}     // ignore other events
            EventKind::Access(_) => {} // ignore access events

            EventKind::Create(_) => ret.push(WatchEvent {
                kind: WatchEventType::Create,
                target: event.paths.get(0).unwrap().clone(),
            }),

            EventKind::Modify(kind) => {
                match kind {
                    ModifyKind::Metadata(_) => {} // ignore metadata changes

                    ModifyKind::Any => ret.push(WatchEvent {
                        kind: WatchEventType::Modify,
                        target: event.paths.get(0).unwrap().clone(),
                    }),

                    ModifyKind::Data(_) => ret.push(WatchEvent {
                        kind: WatchEventType::Modify,
                        target: event.paths.get(0).unwrap().clone(),
                    }),

                    ModifyKind::Name(RenameMode::From) => ret.push(WatchEvent {
                        kind: WatchEventType::Delete,
                        target: event.paths.get(0).unwrap().clone(),
                    }),

                    ModifyKind::Name(RenameMode::To) => ret.push(WatchEvent {
                        kind: WatchEventType::Delete,
                        target: event.paths.get(0).unwrap().clone(),
                    }),

                    ModifyKind::Name(RenameMode::Both) => {
                        // result.paths is in the form [from, to]

                        ret.push(WatchEvent {
                            kind: WatchEventType::Delete,
                            target: event.paths.get(0).unwrap().clone(),
                        });

                        ret.push(WatchEvent {
                            kind: WatchEventType::Create,
                            target: event.paths.get(1).unwrap().clone(),
                        });
                    }

                    _ => {}
                }
            }

            EventKind::Remove(_) => ret.push(WatchEvent {
                kind: WatchEventType::Delete,
                target: event.paths.get(0).unwrap().clone(),
            }),

            _ => {}
        }

        ret
    }
}

#[pymethods]
impl WatchEvent {
    fn __repr__(&self) -> String {
        format!("{self:#?}")
    }
}

#[pyclass(frozen)]
pub struct Watch {
    watcher: Mutex<RecommendedWatcher>,
    rx: Mutex<RefCell<Option<Receiver<NotifyResult<Event>>>>>,
}

#[pymethods]
impl Watch {
    #[new]
    fn new<'py>() -> PyResult<Self> {
        let (tx, rx) = channel::<NotifyResult<Event>>();
        Ok(Self {
            watcher: Mutex::new(
                recommended_watcher(tx)
                    .map_err(|e| PyOSError::new_err(format!("failed to watch: {e}")))?,
            ),
            rx: Mutex::new(RefCell::new(Some(rx))),
        })
    }

    fn add_watch(&self, to_watch: PathBuf, is_recursive: bool) -> PyResult<()> {
        let recursive = if is_recursive {
            RecursiveMode::Recursive
        } else {
            RecursiveMode::NonRecursive
        };
        
        self.watcher
            .try_lock()
            .map_err(|e| PyOSError::new_err(format!("failed to lock watcher: {e}")))?
            .watch(to_watch.as_path(), recursive)
            .map_err(|e| PyOSError::new_err(format!("failed to add watch: {e}")))?;

        Ok(())
    }

    fn __aiter__(slf: PyRef<'_, Self>) -> PyResult<PyRef<Self>> {
        Ok(slf)
    }

    fn __anext__<'py>(slf: Bound<'py, Self>, _py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let asyncio = _py.import_bound("asyncio")?;
        let evloop = asyncio.call_method0("get_event_loop")?;
        let task = evloop.call_method0("create_future")?;

        let rx_cell = match slf.get().rx.try_lock() {
            Ok(lock) => lock,
            Err(e) => {
                return Err(PyRuntimeError::new_err(format!(
                    "the read lock is being held by someone else (mutex error: {e})"
                )))
            }
        };

        let Some(rx) = rx_cell.take() else {
            return Err(PyRuntimeError::new_err(
                "the read lock is being held by something else (the lock was None)",
            ));
        };

        match rx.try_recv() {
            Ok(ev) => {
                let event = ev.map_err(|e| PyOSError::new_err(format!("watch error: {e}")))?;

                rx_cell.replace(Some(rx));
                drop(rx_cell);
                task.call_method1("set_result", (WatchEvent::from_event(event),))?;
            }
            Err(err) => {
                if let TryRecvError::Disconnected = err {
                    return Err(PyRuntimeError::new_err("internal watch error: other half disconnected (maybe the watcher was destroyed)"));
                }

                let slf = slf.clone().unbind();
                let task = task.clone().unbind();
                let evloop = evloop.clone().unbind();

                drop(rx_cell);

                _py.allow_threads(move || {
                    thread::spawn(move || {
                        let result = rx.recv();
                        let lock = slf.get().rx.lock();
                        
                        Python::with_gil(move |_py| {
                            let task = task.bind(_py);
                            let evloop = evloop.bind(_py);
                            
                            match lock {
                                Ok(rx_cell) => {
                                    rx_cell.replace(Some(rx));
                                    drop(rx_cell);
                                    
                                    match result {
                                        Ok(Ok(result)) => evloop.call_method1("call_soon_threadsafe", (task.getattr("set_result").unwrap(), WatchEvent::from_event(result))),
                                        Err(_) => evloop.call_method1("call_soon_threadsafe", (task.getattr("set_exception").unwrap(), PyEnvironmentError::new_err("internal watch error: other half disconnected (maybe the watcher was destroyed)"))),
                                        Ok(Err(e)) => evloop.call_method1("call_soon_threadsafe", (task.getattr("set_exception").unwrap(), PyOSError::new_err(format!("watch error: {e}"))))
                                    }.unwrap();
                                }
                                
                                Err(e) => {
                                    task.call_method1("set_exception", (PyEnvironmentError::new_err(format!("internal error while acquiring lock: {e}")),)).unwrap();
                                }
                            };
                        });
                    });
                });
            }
        };

        Ok(task)
    }
}
