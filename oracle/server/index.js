/**
 * Limitless Oracle — Node.js Backend
 * ====================================
 * Full port of limitless_executor.py using ethers v6 + node-fetch.
 * Serves the React build and handles all Limitless Exchange API calls.
 *
 * Render start command: node server/index.js
 */

import express from "express";
import path from "path";
import { fileURLToPath } from "url";
import crypto from "crypto";
import { ethers } from "ethers";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const app = express();
app.use(express.json());

const PORT      = process.env.PORT || 5000;
const API_BASE  = "https://api.limitless.exchange";
const CHAIN_ID  = 8453n; // Base mainnet — BigInt for ethers v6
const ZERO_ADDR = "0x0000000000000000000000000000000000000000";

// In-memory caches (reset on server restart — fine for 15-min cycle)
const slugCache    = {};
const marketCache  = {};
let   ownerIdCache = null;
let   feeRateCache = null;

const KNOWN_SLUGS = {
  BTC:  "btc-up-or-down-15-min",
  ETH:  "eth-up-or-down-15-min",
  SOL:  "sol-up-or-down-15-min",
  XRP:  "xrp-up-or-down-15-min",
  BNB:  "bnb-up-or-down-15-min",
  DOGE: "doge-up-or-down-15-min",
};

// ══════════════════════════════════════════════════════════════════
// CREDENTIAL HELPERS
// ══════════════════════════════════════════════════════════════════

/** Merge request-body credentials into a working cred object.
 *  Request creds override env vars so dashboard settings win. */
function resolveCreds(body = {}) {
  const c = body.credentials || {};
  return {
    privateKey:   (c.privateKey   || process.env.LIMITLESS_PRIVATE_KEY   || "").trim(),
    tokenId:      (c.tokenId      || process.env.LIMITLESS_TOKEN_ID      || "").trim(),
    tokenSecret:  (c.tokenSecret  || process.env.LIMITLESS_TOKEN_SECRET  || "").trim(),
  };
}

function getSignerAddress(privateKey) {
  if (!privateKey) return null;
  try {
    const wallet = new ethers.Wallet(privateKey);
    return wallet.address;
  } catch { return null; }
}

// ══════════════════════════════════════════════════════════════════
// HMAC AUTH  (mirrors Python _build_hmac_headers exactly)
// ══════════════════════════════════════════════════════════════════

function isoTimestamp() {
  return new Date().toISOString().replace(/(\.\d{3})\d*Z/, "$1Z");
}

function buildHmacHeaders(method, path, body = "", creds) {
  const { tokenId, tokenSecret } = creds;
  if (!tokenId || !tokenSecret) {
    throw new Error("LIMITLESS_TOKEN_ID and LIMITLESS_TOKEN_SECRET are required");
  }
  const ts  = isoTimestamp();
  const msg = `${ts}\n${method}\n${path}\n${body}`;
  const key = Buffer.from(tokenSecret, "base64");
  const sig = crypto.createHmac("sha256", key).update(msg, "utf8").digest("base64");
  return {
    "lmts-api-key":   tokenId,
    "lmts-timestamp": ts,
    "lmts-signature": sig,
    "Content-Type":   "application/json",
  };
}

// ══════════════════════════════════════════════════════════════════
// MARKET DISCOVERY
// ══════════════════════════════════════════════════════════════════

function timeframeRank(slug = "", name = "", freq = "", subFreq = "") {
  const combined = `${slug} ${name} ${freq} ${subFreq}`.toLowerCase();
  if (combined.includes("15-min") || combined.includes("15min") || combined.includes("-15-")) return 0;
  return -1;
}

async function discoverSlugViaActiveSlugs(ticker) {
  const baseTicker   = ticker.toUpperCase().replace("-USDT", "");
  const knownSlug    = KNOWN_SLUGS[baseTicker] || "";

  try {
    const res  = await fetch(`${API_BASE}/markets/active/slugs`, { signal: AbortSignal.timeout(10000) });
    if (!res.ok) return null;
    const entries = await res.json();
    if (!Array.isArray(entries)) return null;

    const matches = []; // [rank, deadline, slug]

    for (const entry of entries) {
      if (typeof entry !== "object" || !entry) continue;

      const entryTicker   = (entry.ticker || "").toUpperCase().replace("-USDT", "");
      const entrySlug     = (entry.slug   || "").toLowerCase();
      const entryDeadline = entry.deadline || "";
      const children      = entry.markets  || [];

      if (!entryTicker) {
        // Group row — check children
        for (const child of children) {
          if (typeof child !== "object" || !child) continue;
          const childTicker   = (child.ticker || "").toUpperCase().replace("-USDT", "");
          const childSlug     = (child.slug   || "").toLowerCase();
          const childDeadline = child.deadline || entryDeadline;

          const tickerMatch = childTicker === baseTicker;
          const slugHint    = !childTicker && (
            baseTicker.toLowerCase() in childSlug ||
            (knownSlug && childSlug.startsWith(knownSlug))
          );
          if (!(tickerMatch || slugHint) || !childSlug) continue;

          const rank = timeframeRank(childSlug, child.title || child.name || "", child.frequency || "", child.subFrequency || "");
          if (rank === -1) continue;
          matches.push([rank, childDeadline, childSlug]);
        }
      } else {
        if (entryTicker !== baseTicker) continue;
        if (children.length > 0) {
          for (const child of children) {
            const childSlug     = (child.slug || "").toLowerCase();
            const childDeadline = child.deadline || entryDeadline;
            if (!childSlug) continue;
            const rank = timeframeRank(childSlug);
            if (rank !== -1) matches.push([rank, childDeadline, childSlug]);
          }
        } else {
          const rank = timeframeRank(entrySlug, entry.title || entry.name || "", entry.frequency || "", entry.subFrequency || "");
          if (rank !== -1) matches.push([rank, entryDeadline, entrySlug]);
        }
      }
    }

    if (matches.length === 0) return null;
    // Sort: rank ASC (0 best), then deadline DESC (latest)
    matches.sort((a, b) => a[0] - b[0] || (b[1] > a[1] ? 1 : -1));
    return matches[0][2];
  } catch (e) {
    console.error("discoverSlugViaActiveSlugs error:", e.message);
    return null;
  }
}

async function discoverSlug(symbol) {
  const ticker = symbol.replace("-USDT", "").toUpperCase();
  for (let attempt = 1; attempt <= 5; attempt++) {
    const slug = await discoverSlugViaActiveSlugs(ticker);
    if (slug) {
      slugCache[symbol] = slug;
      return slug;
    }
    if (attempt < 5) await new Promise(r => setTimeout(r, 10000));
  }
  return null;
}

// ══════════════════════════════════════════════════════════════════
// MARKET DATA
// ══════════════════════════════════════════════════════════════════

async function fetchMarket(slug) {
  if (marketCache[slug]?._orderbook_merged) return marketCache[slug];

  let market = {};

  try {
    const r = await fetch(`${API_BASE}/markets/${slug}`, { signal: AbortSignal.timeout(10000) });
    if (r.status === 404) return null;
    if (r.ok) market = await r.json() || {};
  } catch (e) {
    console.error("fetchMarket base error:", e.message);
  }

  try {
    const r2 = await fetch(`${API_BASE}/markets/${slug}/orderbook`, { signal: AbortSignal.timeout(10000) });
    if (r2.ok) {
      const ob = await r2.json() || {};
      const venue = typeof market.venue === "object" ? market.venue : {};
      const exchangeAddr = venue?.exchange || market.exchange || market.condExchange || null;
      const obYesToken   = ob.tokenId || ob.yesTokenId || null;
      const obNoToken    = ob.noTokenId || null;

      let yesToken = null, noToken = null;
      const baseTokens = market.tokens || {};
      if (Array.isArray(baseTokens)) {
        yesToken = baseTokens.find(t => ["yes","up","1"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId || null;
        noToken  = baseTokens.find(t => ["no","down","0"].includes(String(t?.outcome||"").toLowerCase()))?.tokenId || null;
      } else if (typeof baseTokens === "object") {
        yesToken = baseTokens.yes || baseTokens.Yes || null;
        noToken  = baseTokens.no  || baseTokens.No  || null;
      }
      yesToken = yesToken || obYesToken;
      noToken  = noToken  || obNoToken;

      let posIds = market.positionIds || market.position_ids || [];
      if (posIds.length === 0 && yesToken) posIds = [yesToken, ...(noToken ? [noToken] : [])];

      const conditionId = market.conditionId || market.condition_id || market.ctfConditionId || market.condId || null;

      Object.assign(market, {
        slug,
        exchange:          exchangeAddr,
        venue:             { exchange: exchangeAddr },
        tokens:            { yes: yesToken, no: noToken },
        positionIds:       posIds,
        conditionId,
        _orderbook_merged: true,
      });
    }
  } catch (e) {
    console.error("fetchMarket orderbook error:", e.message);
  }

  if (!market || Object.keys(market).length === 0) return null;
  market.slug = market.slug || slug;
  marketCache[slug] = market;
  return market;
}

function extractExchange(market) {
  if (!market) return null;
  const venue = market.venue;
  if (typeof venue === "object") return venue.exchange || venue.condExchange || null;
  return market.exchange || market.condExchange || null;
}

function extractTokenId(market, direction) {
  if (!market) return null;
  const tokens = market.tokens || {};
  if (typeof tokens === "object") {
    const tid = direction === "UP" ? tokens.yes : tokens.no;
    if (tid) return String(tid);
  }
  const posIds = market.positionIds || [];
  if (posIds.length > 0) {
    if (direction === "UP") return String(posIds[0]);
    if (direction === "DOWN" && posIds.length > 1) return String(posIds[1]);
  }
  return null;
}

// ══════════════════════════════════════════════════════════════════
// OWNER ID + FEE RATE
// ══════════════════════════════════════════════════════════════════

async function getOwnerIdAndFee(makerAddr, creds) {
  if (ownerIdCache !== null && feeRateCache !== null) {
    return { ownerId: ownerIdCache, feeRateBps: feeRateCache };
  }
  try {
    const path    = `/profiles/${makerAddr}`;
    const headers = buildHmacHeaders("GET", path, "", creds);
    const res     = await fetch(`${API_BASE}${path}`, { headers, signal: AbortSignal.timeout(10000) });
    if (res.ok) {
      const data = await res.json();
      const id   = data.id != null ? Number(data.id) : null;
      const fee  = data.rank?.feeRateBps != null ? Number(data.rank.feeRateBps) : 0;
      if (id !== null) {
        ownerIdCache  = id;
        feeRateCache  = fee;
        return { ownerId: id, feeRateBps: fee };
      }
    }
  } catch (e) {
    console.error("getOwnerIdAndFee error:", e.message);
  }
  return { ownerId: null, feeRateBps: 0 };
}

// ══════════════════════════════════════════════════════════════════
// EIP-712 SIGNING  (ethers v6)
// ══════════════════════════════════════════════════════════════════

const ORDER_TYPES = {
  Order: [
    { name: "salt",          type: "uint256" },
    { name: "maker",         type: "address" },
    { name: "signer",        type: "address" },
    { name: "taker",         type: "address" },
    { name: "tokenId",       type: "uint256" },
    { name: "makerAmount",   type: "uint256" },
    { name: "takerAmount",   type: "uint256" },
    { name: "expiration",    type: "uint256" },
    { name: "nonce",         type: "uint256" },
    { name: "feeRateBps",    type: "uint256" },
    { name: "side",          type: "uint8"   },
    { name: "signatureType", type: "uint8"   },
  ],
};

async function signOrder(order, exchangeAddr, privateKey) {
  const wallet = new ethers.Wallet(privateKey);
  const domain = {
    name:              "Limitless CTF Exchange",
    version:           "1",
    chainId:           CHAIN_ID,
    verifyingContract: ethers.getAddress(exchangeAddr),
  };
  // ethers v6: all uint256 fields must be BigInt
  const values = {
    salt:          BigInt(order.salt),
    maker:         ethers.getAddress(order.maker),
    signer:        ethers.getAddress(order.signer),
    taker:         ethers.getAddress(order.taker),
    tokenId:       BigInt(order.tokenId),
    makerAmount:   BigInt(order.makerAmount),
    takerAmount:   BigInt(order.takerAmount),
    expiration:    BigInt(order.expiration),
    nonce:         BigInt(order.nonce),
    feeRateBps:    BigInt(order.feeRateBps),
    side:          Number(order.side),
    signatureType: Number(order.signatureType),
  };
  return wallet.signTypedData(domain, ORDER_TYPES, values);
}

// ══════════════════════════════════════════════════════════════════
// PLACE LIVE ORDER
// ══════════════════════════════════════════════════════════════════

async function placeLiveOrder(symbol, direction, positionSizeUsd, maxContractPrice, creds) {
  const { privateKey } = creds;
  if (!privateKey) return { success: false, error: "LIMITLESS_PRIVATE_KEY not set" };

  let makerAddr;
  try { makerAddr = new ethers.Wallet(privateKey).address; }
  catch (e) { return { success: false, error: `Invalid private key: ${e.message}` }; }

  // Fresh slug
  delete slugCache[symbol];
  const slug = await discoverSlug(symbol);
  if (!slug) return { success: false, error: `No active 15-min market for ${symbol}` };

  // Fresh market data
  delete marketCache[slug];
  const market = await fetchMarket(slug);
  if (!market) return { success: false, error: `Cannot fetch market for slug=${slug}` };

  const exchangeAddr = extractExchange(market);
  if (!exchangeAddr) return { success: false, error: "venue.exchange missing from market" };

  const tokenId = extractTokenId(market, direction);
  if (!tokenId) return { success: false, error: `Token ID missing for direction=${direction}` };

  const { ownerId, feeRateBps } = await getOwnerIdAndFee(makerAddr, creds);
  if (ownerId === null) return { success: false, error: `Could not resolve ownerId for ${makerAddr}` };

  const price       = Math.round(Math.min(maxContractPrice, 0.99) * 100) / 100;
  const size        = Math.round((positionSizeUsd / price) * 10000) / 10000;
  const makerAmount = Math.round(price * size * 1_000_000);
  const takerAmount = Math.round(size * 1_000_000);
  const salt        = BigInt(Date.now());

  const order = {
    salt:          salt.toString(),
    maker:         makerAddr,
    signer:        makerAddr,   // EOA: maker = signer
    taker:         ZERO_ADDR,
    tokenId:       tokenId,
    makerAmount,
    takerAmount,
    expiration:    "0",
    nonce:         0,
    feeRateBps,
    side:          0,   // BUY
    signatureType: 0,   // EOA
  };

  let signature;
  try {
    signature = await signOrder(order, exchangeAddr, privateKey);
  } catch (e) {
    return { success: false, error: `EIP-712 signing failed: ${e.message}` };
  }

  const payload = {
    order: { ...order, signature, signatureType: 0, price },
    orderType:  "GTC",
    marketSlug: slug,
    ownerId,
  };

  const bodyStr = JSON.stringify(payload);
  let headers;
  try { headers = buildHmacHeaders("POST", "/orders", bodyStr, creds); }
  catch (e) { return { success: false, error: e.message }; }

  try {
    const res = await fetch(`${API_BASE}/orders`, {
      method:  "POST",
      headers,
      body:    bodyStr,
      signal:  AbortSignal.timeout(15000),
    });

    const text = await res.text();
    console.log(`[${symbol}] POST /orders → ${res.status}: ${text.slice(0, 400)}`);

    if (!res.ok) {
      let detail;
      try { detail = JSON.parse(text); } catch { detail = text; }
      return { success: false, http_status: res.status, error: `HTTP ${res.status}`, api_response: detail };
    }

    const result  = JSON.parse(text);
    const orderId = result?.order?.id || result?.id || result?.orderId || salt.toString();

    return {
      success:            true,
      order_id:           String(orderId),
      contracts:          size,
      price_per_contract: price,
      total_spent:        positionSizeUsd,
      slug,
      condition_id:       market.conditionId || market.condition_id || market.condId || null,
      signal_direction:   direction,
      trade_direction:    direction,
      maker:              makerAddr,
    };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

function placeShadowOrder(symbol, direction, positionSizeUsd, maxContractPrice) {
  const price = Math.min(maxContractPrice, 0.99);
  const size  = Math.round((positionSizeUsd / price) * 10000) / 10000;
  return {
    success:            true,
    order_id:           `shadow_${Date.now()}`,
    contracts:          size,
    price_per_contract: price,
    total_spent:        positionSizeUsd,
    signal_direction:   direction,
    trade_direction:    direction,
    shadow:             true,
  };
}

// ══════════════════════════════════════════════════════════════════
// CLAIM WINNINGS
// ══════════════════════════════════════════════════════════════════

async function claimWinnings(marketSlug, symbol, condId, creds) {
  let conditionId = condId;

  if (!conditionId && marketSlug) {
    delete marketCache[marketSlug];
    const mkt = await fetchMarket(marketSlug);
    if (mkt) conditionId = mkt.conditionId || mkt.condition_id || mkt.ctfConditionId || mkt.condId;
  }

  if (!conditionId) {
    return { success: false, error: `conditionId not found for ${marketSlug}` };
  }

  const path    = "/portfolio/redeem";
  const bodyStr = JSON.stringify({ conditionId: String(conditionId) });
  let headers;
  try { headers = buildHmacHeaders("POST", path, bodyStr, creds); }
  catch (e) { return { success: false, error: e.message }; }

  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method:  "POST",
      headers,
      body:    bodyStr,
      signal:  AbortSignal.timeout(15000),
    });
    const text = await res.text();
    if (!res.ok) {
      let err; try { err = JSON.parse(text); } catch { err = text; }
      return { success: false, http_status: res.status, error: `HTTP ${res.status}`, api_response: err };
    }
    const result = JSON.parse(text) || {};
    return { success: true, redeemed_amount: result.amount || result.redeemed || null, raw: result };
  } catch (e) {
    return { success: false, error: e.message };
  }
}

// ══════════════════════════════════════════════════════════════════
// CHECK ORDER FILLED
// ══════════════════════════════════════════════════════════════════

async function checkOrderFilled(marketSlug, orderId, creds) {
  if (!marketSlug || !orderId) return { filled: false, status: "ERROR", error: "missing slug or orderId" };
  if (String(orderId).startsWith("shadow_")) return { filled: false, status: "SHADOW" };

  const path    = `/markets/${marketSlug}/user-orders`;
  const params  = "?statuses=MATCHED&statuses=LIVE&limit=100";
  let headers;
  try { headers = buildHmacHeaders("GET", path, "", creds); }
  catch (e) { return { filled: false, status: "ERROR", error: e.message }; }

  try {
    const res  = await fetch(`${API_BASE}${path}${params}`, { headers, signal: AbortSignal.timeout(10000) });
    if (!res.ok) return { filled: false, status: "ERROR", error: `HTTP ${res.status}` };
    const data   = await res.json() || {};
    const orders = data.orders || [];
    for (const o of orders) {
      if (String(o.id) === String(orderId)) {
        const status = String(o.status || "").toUpperCase();
        return { filled: status === "MATCHED", status };
      }
    }
    return { filled: false, status: "NOT_FOUND" };
  } catch (e) {
    return { filled: false, status: "ERROR", error: e.message };
  }
}

// ══════════════════════════════════════════════════════════════════
// VALIDATE CREDENTIALS
// ══════════════════════════════════════════════════════════════════

function validateCreds(creds) {
  const { privateKey, tokenId, tokenSecret } = creds;
  const signerAddr = getSignerAddress(privateKey);
  return {
    hmac_auth_ready:    !!(tokenId && tokenSecret),
    signing_ready:      !!privateKey,
    live_trading_ready: !!(tokenId && tokenSecret && privateKey),
    signer_address:     signerAddr,
    maker_address:      signerAddr,
    LIMITLESS_TOKEN_ID:     !!tokenId,
    LIMITLESS_TOKEN_SECRET: !!tokenSecret,
    LIMITLESS_PRIVATE_KEY:  !!privateKey,
  };
}

// ══════════════════════════════════════════════════════════════════
// EXPRESS API ROUTES
// ══════════════════════════════════════════════════════════════════

app.get("/api/ping", (_req, res) => res.json({ ok: true, ts: Date.now() }));

app.post("/api/limitless/execute", async (req, res) => {
  try {
    const { symbol = "BTC-USDT", direction = "UP", mode = "shadow",
            positionSize = 10, maxContractPrice = 0.50 } = req.body;
    const creds = resolveCreds(req.body);

    let result;
    if (mode === "live") {
      result = await placeLiveOrder(symbol, direction, Number(positionSize), Number(maxContractPrice), creds);
    } else {
      result = placeShadowOrder(symbol, direction, Number(positionSize), Number(maxContractPrice));
    }
    console.log(`[execute] [${mode}] ${symbol} ${direction} → success=${result.success}`);
    res.json(result);
  } catch (e) {
    console.error("execute error:", e);
    res.status(500).json({ success: false, error: e.message });
  }
});

app.post("/api/limitless/claim", async (req, res) => {
  try {
    const { marketSlug = "", symbol = "", conditionId, credentials } = req.body;
    const creds  = resolveCreds(req.body);
    const result = await claimWinnings(marketSlug, symbol, conditionId, creds);
    res.json(result);
  } catch (e) {
    res.status(500).json({ success: false, error: e.message });
  }
});

app.post("/api/limitless/order-status", async (req, res) => {
  try {
    const { marketSlug = "", orderId = "" } = req.body;
    const creds  = resolveCreds(req.body);
    const result = await checkOrderFilled(marketSlug, orderId, creds);
    res.json(result);
  } catch (e) {
    res.status(500).json({ success: false, error: e.message });
  }
});

app.post("/api/limitless/validate", (req, res) => {
  try {
    const creds  = resolveCreds(req.body);
    res.json(validateCreds(creds));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get("/api/limitless/slug/:symbol", async (req, res) => {
  try {
    const slug = await discoverSlug(req.params.symbol);
    res.json({ symbol: req.params.symbol, slug });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ══════════════════════════════════════════════════════════════════
// SERVE REACT BUILD (catch-all — must be last)
// ══════════════════════════════════════════════════════════════════

const CLIENT_DIST = path.join(__dirname, "..", "client", "dist");
app.use(express.static(CLIENT_DIST));
app.get("*", (_req, res) => {
  res.sendFile(path.join(CLIENT_DIST, "index.html"));
});

// ══════════════════════════════════════════════════════════════════
// START
// ══════════════════════════════════════════════════════════════════

app.listen(PORT, () => {
  console.log(`✅ Limitless Oracle server running on port ${PORT}`);
  console.log(`   Live trading ready: ${!!(process.env.LIMITLESS_TOKEN_ID && process.env.LIMITLESS_PRIVATE_KEY)}`);
});
