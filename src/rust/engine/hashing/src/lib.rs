// Copyright 2017 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

// Enable all clippy lints except for many of the pedantic ones. It's a shame this needs to be copied and pasted across crates, but there doesn't appear to be a way to include inner attributes from a common source.
#![cfg_attr(
  feature = "cargo-clippy",
  deny(
    clippy, default_trait_access, expl_impl_clone_on_copy, if_not_else, needless_continue,
    single_match_else, unseparated_literal_suffix, used_underscore_binding
  )
)]
// It is often more clear to show that nothing is being moved.
#![cfg_attr(feature = "cargo-clippy", allow(match_ref_pats))]
// Subjective style.
#![cfg_attr(feature = "cargo-clippy", allow(len_without_is_empty, redundant_field_names))]
// Default isn't as big a deal as people seem to think it is.
#![cfg_attr(feature = "cargo-clippy", allow(new_without_default, new_without_default_derive))]
// Arc<Mutex> can be more clear than needing to grok Orderings:
#![cfg_attr(feature = "cargo-clippy", allow(mutex_atomic))]

extern crate digest;
extern crate hex;
extern crate sha2;

use digest::{Digest as DigestTrait, FixedOutput};
use sha2::Sha256;

use std::fmt;
use std::io::{self, Write};

const FINGERPRINT_SIZE: usize = 32;

#[derive(Clone, Copy, Eq, Hash, PartialEq, Ord, PartialOrd)]
pub struct Fingerprint(pub [u8; FINGERPRINT_SIZE]);

impl Fingerprint {
  pub fn from_bytes_unsafe(bytes: &[u8]) -> Fingerprint {
    if bytes.len() != FINGERPRINT_SIZE {
      panic!(
        "Input value was not a fingerprint; had length: {}",
        bytes.len()
      );
    }

    let mut fingerprint = [0; FINGERPRINT_SIZE];
    fingerprint.clone_from_slice(&bytes[0..FINGERPRINT_SIZE]);
    Fingerprint(fingerprint)
  }

  pub fn from_hex_string(hex_string: &str) -> Result<Fingerprint, String> {
    <[u8; FINGERPRINT_SIZE] as hex::FromHex>::from_hex(hex_string)
      .map(Fingerprint)
      .map_err(|e| format!("{:?}", e))
  }

  pub fn as_bytes(&self) -> &[u8; FINGERPRINT_SIZE] {
    &self.0
  }

  pub fn to_hex(&self) -> String {
    let mut s = String::new();
    for &byte in &self.0 {
      fmt::Write::write_fmt(&mut s, format_args!("{:02x}", byte)).unwrap();
    }
    s
  }
}

impl fmt::Display for Fingerprint {
  fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
    write!(f, "{}", self.to_hex())
  }
}

impl fmt::Debug for Fingerprint {
  fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
    write!(f, "Fingerprint<{}>", self.to_hex())
  }
}

impl AsRef<[u8]> for Fingerprint {
  fn as_ref(&self) -> &[u8] {
    &self.0[..]
  }
}

///
/// A Digest is a fingerprint, as well as the size in bytes of the plaintext for which that is the
/// fingerprint.
///
/// It is equivalent to a Bazel Remote Execution Digest, but without the overhead (and awkward API)
/// of needing to create an entire protobuf to pass around the two fields.
///
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct Digest(pub Fingerprint, pub usize);

///
/// A Write instance that fingerprints all data that passes through it.
///
pub struct WriterHasher<W: Write> {
  hasher: Sha256,
  inner: W,
}

impl<W: Write> WriterHasher<W> {
  pub fn new(inner: W) -> WriterHasher<W> {
    WriterHasher {
      hasher: Sha256::default(),
      inner: inner,
    }
  }

  ///
  /// Returns the result of fingerprinting this stream, and Drops the stream.
  ///
  pub fn finish(self) -> Fingerprint {
    Fingerprint::from_bytes_unsafe(&self.hasher.fixed_result())
  }
}

impl<W: Write> Write for WriterHasher<W> {
  fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
    let written = self.inner.write(buf)?;
    // Hash the bytes that were successfully written.
    self.hasher.input(&buf[0..written]);
    Ok(written)
  }

  fn flush(&mut self) -> io::Result<()> {
    self.inner.flush()
  }
}

#[cfg(test)]
mod fingerprint_tests {
  use super::Fingerprint;

  #[test]
  fn from_bytes_unsafe() {
    assert_eq!(
      Fingerprint::from_bytes_unsafe(&[
        0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab,
        0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab, 0xab,
        0xab, 0xab,
      ],),
      Fingerprint([0xab; 32])
    );
  }

  #[test]
  fn from_hex_string() {
    assert_eq!(
      Fingerprint::from_hex_string(
        "0123456789abcdefFEDCBA98765432100000000000000000ffFFfFfFFfFfFFff",
      ).unwrap(),
      Fingerprint([
        0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32,
        0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
        0xff, 0xff,
      ],)
    )
  }

  #[test]
  fn from_hex_string_not_long_enough() {
    Fingerprint::from_hex_string("abcd").expect_err("Want err");
  }

  #[test]
  fn from_hex_string_too_long() {
    Fingerprint::from_hex_string(
      "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0",
    ).expect_err("Want err");
  }

  #[test]
  fn from_hex_string_invalid_chars() {
    Fingerprint::from_hex_string(
      "Q123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF",
    ).expect_err("Want err");
  }

  #[test]
  fn to_hex() {
    assert_eq!(
      Fingerprint([
        0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0xfe, 0xdc, 0xba, 0x98, 0x76, 0x54, 0x32,
        0x10, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
        0xff, 0xff,
      ],)
        .to_hex(),
      "0123456789abcdeffedcba98765432100000000000000000ffffffffffffffff".to_lowercase()
    )
  }

  #[test]
  fn display() {
    let hex = "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF";
    assert_eq!(
      Fingerprint::from_hex_string(hex).unwrap().to_hex(),
      hex.to_lowercase()
    )
  }
}
