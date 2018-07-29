extern crate env_logger;
extern crate futures;
extern crate futures_timer;
extern crate h2;
extern crate http;
extern crate prost;
#[macro_use]
extern crate prost_derive;
extern crate prost_types;
extern crate tokio_core;
extern crate tower_grpc;
extern crate tower_h2;
extern crate tower_http;

use futures::{stream::poll_fn, Async, Future, Poll, Stream};
use std::sync::mpsc::channel;
use std::sync::{Arc, Mutex};
use tokio_core::net::{TcpListener, TcpStream};
use tokio_core::reactor::Core;
use tower_grpc::codegen::server::tower::NewService;
use tower_grpc::{Request, Response};
use tower_h2::Body;
use tower_h2::RecvBody;
use tower_h2::{client::Connection, Server};

pub mod build {
  pub mod bazel {
    pub mod remote {
      pub mod execution {
        pub mod v2 {
          include!(concat!(
            env!("OUT_DIR"),
            "/build.bazel.remote.execution.v2.rs"
          ));
        }
      }
    }
  }
}

pub mod google {
  pub mod api {
    include!(concat!(env!("OUT_DIR"), "/google.api.rs"));
  }

  pub mod longrunning {
    include!(concat!(env!("OUT_DIR"), "/google.longrunning.rs"));
  }

  pub mod rpc {
    include!(concat!(env!("OUT_DIR"), "/google.rpc.rs"));
  }

  pub mod protobuf {
    include!(concat!(env!("OUT_DIR"), "/google.protobuf.rs"));

    pub type Empty = ();
  }
}

#[derive(Clone)]
struct CAS;

impl build::bazel::remote::execution::v2::server::ContentAddressableStorage for CAS {
  type FindMissingBlobsFuture = futures::future::FutureResult<
    tower_grpc::Response<build::bazel::remote::execution::v2::FindMissingBlobsResponse>,
    tower_grpc::Error,
  >;
  type BatchUpdateBlobsFuture = futures::future::FutureResult<
    tower_grpc::Response<build::bazel::remote::execution::v2::BatchUpdateBlobsResponse>,
    tower_grpc::Error,
  >;
  type GetTreeStream = futures::stream::IterOk<
    std::vec::IntoIter<build::bazel::remote::execution::v2::GetTreeResponse>,
    tower_grpc::Error,
  >;
  type GetTreeFuture =
    futures::future::FutureResult<tower_grpc::Response<Self::GetTreeStream>, tower_grpc::Error>;

  fn find_missing_blobs(
    &mut self,
    request: tower_grpc::Request<build::bazel::remote::execution::v2::FindMissingBlobsRequest>,
  ) -> Self::FindMissingBlobsFuture {
    println!("DWH: Got find missing blobs");
    futures::future::ok(Response::new(
      build::bazel::remote::execution::v2::FindMissingBlobsResponse {
        missing_blob_digests: vec![],
      },
    ))
  }

  fn batch_update_blobs(
    &mut self,
    request: tower_grpc::Request<build::bazel::remote::execution::v2::BatchUpdateBlobsRequest>,
  ) -> Self::BatchUpdateBlobsFuture {
    unimplemented!();
  }

  fn get_tree(
    &mut self,
    request: tower_grpc::Request<build::bazel::remote::execution::v2::GetTreeRequest>,
  ) -> Self::GetTreeFuture {
    unimplemented!();
  }
}

fn main() {
  let cas = CAS;
  let new_service =
    build::bazel::remote::execution::v2::server::ContentAddressableStorageServer::new(cas);
  {
    let server = StopOnDropServer::new(new_service).expect("Starting");
    println!("DWH: Server started ({:?}); sleeping", server.local_addr());
    std::thread::sleep(std::time::Duration::from_secs(10));
    println!("DWH: Done sleeping");
  }
}

struct StopOnDropServer {
  dropped: Arc<Mutex<bool>>,
  local_addr: std::net::SocketAddr,
}

impl StopOnDropServer {
  pub fn new<S, B, IE, SE>(new_service: S) -> std::io::Result<StopOnDropServer>
  where
    S: NewService<
        Request = http::Request<RecvBody>,
        Response = http::Response<B>,
        InitError = IE,
        Error = SE,
      >
      + Send
      + 'static,
    B: Body + 'static,
    IE: std::fmt::Debug,
    SE: std::fmt::Debug,
  {
    let stop = Arc::new(Mutex::new(false));
    let stop2 = stop.clone();

    let (sender, receiver) = channel();

    std::thread::spawn(move || {
      let addr = "127.0.0.1:0".parse().unwrap();
      let result = (|| {
        let core = Core::new()?;
        let listener = TcpListener::bind(&addr, &core.handle())?;
        let addr = listener.local_addr()?;
        Ok((core, listener, addr))
      })();
      let (mut core, listener) = match result {
        Ok((core, listener, addr)) => {
          sender.send(Ok(addr));
          (core, listener)
        }
        Err(err) => {
          sender
            .send(Err(err))
            .expect("Error sending from started server thread");
          return;
        }
      };

      // Select from three streams:
      //  * Stream 1 gets requests from the network, and continues listening.
      //  * Stream 2 gets a signal that the server has been dropped, and should stop listening.
      //  * Stream 3 gets a re-poll signal at a fixed interval, so that stream 2 is re-polled.
      let serve = listener
        .incoming()
        .map_err(|err| TerminateOrError::Error(err))
        .map(|(sock, _)| HandleOrTerminate::SocketRequest(sock))
        .select(poll_fn(
          move || -> Poll<Option<HandleOrTerminate>, TerminateOrError> {
            if *stop.lock().unwrap() {
              Ok(Async::Ready(Some(HandleOrTerminate::Terminate)))
            } else {
              Ok(Async::NotReady)
            }
          },
        ))
        .select(futures::stream::unfold((), |()| {
          Some(
            // Check for whether the server has been dropped evert 50ms.
            futures_timer::Delay::new(std::time::Duration::from_millis(50))
              .map(|()| (HandleOrTerminate::Continue, ()))
              .map_err(|err| TerminateOrError::Error(err)),
          )
        }))
        .fold(
          (
            Server::new(new_service, h2::server::Builder::default(), core.handle()),
            core.handle(),
          ),
          |(server, reactor), req_or_die| match req_or_die {
            HandleOrTerminate::SocketRequest(sock) => {
              if let Err(e) = sock.set_nodelay(true) {
                return Err(TerminateOrError::Error(e));
              }
              let serve = server.serve(sock);
              reactor.spawn(serve.map_err(|e| error!("Error serving: {:?}", e)));

              Ok((server, reactor))
            }
            HandleOrTerminate::Terminate => Err(TerminateOrError::Terminate),
            HandleOrTerminate::Continue => Ok((server, reactor)),
          },
        );

      core
        .run(serve)
        .map(|_| ())
        .or_else(|result| match result {
          TerminateOrError::Terminate => Ok(()),
          TerminateOrError::Error(err) => Err(err),
        })
        .expect("Error from server");
    });

    match receiver.recv() {
      Ok(Ok(local_addr)) => Ok(StopOnDropServer {
        dropped: stop2,
        local_addr: local_addr,
      }),
      Ok(Err(err)) => Err(err),
      Err(err) => Err(std::io::Error::new(
        std::io::ErrorKind::BrokenPipe,
        "Error starting or while serving server",
      )),
    }
  }

  pub fn local_addr(&self) -> std::net::SocketAddr {
    self.local_addr
  }
}

impl Drop for StopOnDropServer {
  fn drop(&mut self) {
    *self.dropped.lock().unwrap() = true;
  }
}

enum HandleOrTerminate {
  // Indicates a request which should be served has been received.
  SocketRequest(tokio_core::net::TcpStream),
  // Indicates the server should cleanly terminate.
  Terminate,
  // Continue waiting for either of the other conditions.
  Continue,
}

enum TerminateOrError {
  // Indicates the server should cleanly terminate.
  Terminate,
  // Indicates an error occurred, and the the server terminated in the background.
  Error(std::io::Error),
}
