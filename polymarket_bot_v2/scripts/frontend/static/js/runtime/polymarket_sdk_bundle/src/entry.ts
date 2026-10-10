/* T907 — Single-file bundle entry. Re-exports the official Polymarket TS SDKs
 * so the browser can `import('/v2/static/js/runtime/polymarket_sdk_bundle.js')`
 * and obtain everything polymarket_session.js needs.
 *
 * Refs:
 *   https://docs.polymarket.com/trading/deposit-wallets
 *   https://github.com/Polymarket/privy-safe-builder-example
 */
export {
    RelayClient,
    RelayerTransactionState,
} from "@polymarket/builder-relayer-client";

export {
    ClobClient,
    SignatureTypeV2,
    AssetType,
    OrderType,
    Side,
} from "@polymarket/clob-client-v2";

export {
    BuilderConfig,
} from "@polymarket/builder-signing-sdk";

export {
    createWalletClient,
    custom,
    encodeFunctionData,
} from "viem";

export { polygon } from "viem/chains";
