package main

// buildRelayURL, buildClientToken and buildIdentitySecret are the values a
// customized single-binary build stamps in at link time, e.g.:
//
//	go build -ldflags "-X main.buildRelayURL=wss://relay.example.com/ws/client \
//	                    -X main.buildClientToken=s3cr3t \
//	                    -X main.buildIdentitySecret=deployment-salt"
//
// (see client/Makefile's RELAY_URL/CLIENT_TOKEN/IDENTITY_SECRET variables).
// parseConfig resolves each corresponding setting as flag > env var >
// this compiled default, so a customized binary
// launches with zero command-line arguments while a generic build — these
// three vars left at their zero value "" — keeps requiring --url/--token
// exactly as before.
//
// SECURITY, spelled out plainly: a value baked in here via -ldflags is
// trivially recoverable from the resulting binary with a plain `strings`
// (or any hex viewer/disassembler) — -ldflags -X does not obfuscate or
// encrypt, it just patches a string variable's initial value into the
// binary's data section. Embedding a token at compile time is a deliberate
// ergonomics trade-off (the end user double-clicks a ready-to-run
// executable, nothing to type or configure) and NOT a secrecy mechanism.
// The DISTRIBUTED BINARY is therefore itself a secret: handle and store it
// with the same care as the token/URL/identity secret it carries
// (docs/SECURITY.md).
var (
	buildRelayURL       string
	buildClientToken    string
	buildIdentitySecret string
)
