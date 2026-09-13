//! The `/paid` switch: flip the FreeRide Hedera cash lane mid-session.
//!
//! Free-first means the paid lane is invisible whenever free inference works.
//! That is the product being correct, and it is also why the lane is hard to
//! show: the only other way is to delete the operator's provider keys. The
//! daemon keeps this switch in process memory, so a demo can go free, then
//! paid, then free again without restarting anything.
//!
//! Deliberately process-local on the daemon side — a restart returns to
//! whatever `~/.freeride/.env` says, so an interrupted demo cannot leave the
//! wallet paying for every request.

const std = @import("std");
const io_mod = @import("../shared/io.zig");

const policy_url = "http://127.0.0.1:11343/v1/_freeride/x402/policy";
const toggle_url = "http://127.0.0.1:11343/v1/_freeride/x402/force-paid";
const request_timeout_ms = 8_000;

pub const Error = error{
    DaemonUnreachable,
    WalletNotReady,
    OutOfMemory,
};

/// Reads a top-level boolean out of a small flat JSON object.
///
/// The daemon's replies here are a handful of scalars, so scanning for the
/// key beats pulling a parser into this path.
fn boolField(body: []const u8, key: []const u8) ?bool {
    var needle_buf: [64]u8 = undefined;
    const needle = std.fmt.bufPrint(&needle_buf, "\"{s}\"", .{key}) catch return null;
    const at = std.mem.find(u8, body, needle) orelse return null;
    var i = at + needle.len;
    while (i < body.len and (body[i] == ' ' or body[i] == ':')) : (i += 1) {}
    if (std.mem.startsWith(u8, body[i..], "true")) return true;
    if (std.mem.startsWith(u8, body[i..], "false")) return false;
    return null;
}

/// Reads a top-level string out of the same flat replies. Returns a slice of
/// `body`, so it lives exactly as long as the response buffer.
fn stringField(body: []const u8, key: []const u8) ?[]const u8 {
    var needle_buf: [64]u8 = undefined;
    const needle = std.fmt.bufPrint(&needle_buf, "\"{s}\"", .{key}) catch return null;
    const at = std.mem.find(u8, body, needle) orelse return null;
    var i = at + needle.len;
    while (i < body.len and (body[i] == ' ' or body[i] == ':')) : (i += 1) {}
    if (i >= body.len or body[i] != '"') return null;
    i += 1;
    const start = i;
    while (i < body.len and body[i] != '"') : (i += 1) {}
    if (i >= body.len) return null;
    return body[start..i];
}

/// The daemon's replies here are a few dozen bytes; a fixed buffer keeps this
/// path allocation-free and bounds a misbehaving response.
fn fetch(alloc: std.mem.Allocator, url: []const u8, payload: ?[]const u8, sink: []u8) Error![]const u8 {
    var client: std.http.Client = .{ .allocator = alloc, .io = io_mod.getIo() };
    defer client.deinit();

    var writer = std.Io.Writer.fixed(sink);
    const result = client.fetch(.{
        .location = .{ .url = url },
        .method = if (payload == null) .GET else .POST,
        .payload = payload,
        .headers = .{ .content_type = .{ .override = "application/json" } },
        .response_writer = &writer,
    }) catch return Error.DaemonUnreachable;

    if (result.status == .conflict) return Error.WalletNotReady;
    if (result.status != .ok) return Error.DaemonUnreachable;
    return writer.buffered();
}

/// Turn the paid lane on, off, or (with `want == null`) just report it.
///
/// Writes a one-line summary into `out` and returns the slice, so the caller
/// can hand it straight to a notice without owning another allocation.
pub fn apply(alloc: std.mem.Allocator, want: ?bool, out: []u8) Error![]const u8 {
    var sink: [2048]u8 = undefined;

    const on = if (want) |w| blk: {
        var payload_buf: [64]u8 = undefined;
        const payload = std.fmt.bufPrint(
            &payload_buf,
            "{{\"on\":{s}}}",
            .{if (w) "true" else "false"},
        ) catch return Error.OutOfMemory;
        const body = try fetch(alloc, toggle_url, payload, &sink);
        break :blk boolField(body, "force_paid") orelse w;
    } else blk: {
        const body = try fetch(alloc, policy_url, null, &sink);
        break :blk boolField(body, "force_paid") orelse false;
    };

    // The most recent receipt, if the daemon has one. Shown as a quiet
    // trailer rather than a banner: the payment is the point, not the news.
    var receipt: [180]u8 = undefined;
    var receipt_len: usize = 0;
    {
        var policy_sink: [2048]u8 = undefined;
        if (fetch(alloc, policy_url, null, &policy_sink)) |policy_body| {
            if (stringField(policy_body, "transaction")) |tx| {
                const amount = stringField(policy_body, "amount_hbar") orelse "";
                const trailer = std.fmt.bufPrint(
                    &receipt,
                    "\n  last payment {s} HBAR · {s}",
                    .{ amount, tx },
                ) catch "";
                receipt_len = trailer.len;
            }
        } else |_| {}
    }

    const text = if (on)
        "Paid lane ON. Every request settles on Hedera; free providers are skipped. /paid off to stop."
    else
        "Paid lane off. Free providers first; Hedera pays only when free cannot serve.";

    if (text.len + receipt_len > out.len) return Error.OutOfMemory;
    @memcpy(out[0..text.len], text);
    if (receipt_len > 0) @memcpy(out[text.len..][0..receipt_len], receipt[0..receipt_len]);
    return out[0 .. text.len + receipt_len];
}

test "boolField reads the daemon's flat replies" {
    try std.testing.expectEqual(
        @as(?bool, true),
        boolField("{\"ok\":true,\"force_paid\":true,\"persisted\":false}", "force_paid"),
    );
    try std.testing.expectEqual(
        @as(?bool, false),
        boolField("{\"ok\":true,\"force_paid\":false}", "force_paid"),
    );
    try std.testing.expectEqual(
        @as(?bool, null),
        boolField("{\"ok\":true}", "force_paid"),
    );
    // Must not confuse a different key that shares a prefix.
    try std.testing.expectEqual(
        @as(?bool, false),
        boolField("{\"force_paid_note\":true,\"force_paid\":false}", "force_paid"),
    );
}
