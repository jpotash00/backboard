/**
 * Optional React binding — `import { useOffboardCancelFlow } from "offboard/react"`.
 *
 * `react` is a peer dependency: this module only pulls it in for React hosts, and the core
 * `offboard` import stays framework-free. The SDK still owns the modal DOM (§7); this hook is
 * just an idiomatic, stable trigger so a React cancel button is one line:
 *
 *   const cancel = useOffboardCancelFlow({
 *     publicKey: "pk_live_…",           // optional here if you called Offboard.init() already
 *     userId: user.id,
 *     identityToken: user.offboardToken, // if your key uses identity verification
 *     onAccept: (o) => applyOffer(o),
 *     onCancel: () => finishCancellation(),
 *   });
 *   return <button onClick={cancel}>Cancel subscription</button>;
 */

import { useCallback, useRef } from "react";

import Offboard from "./index.js";
import type { InitOptions, ShowCancelFlowOptions } from "./types.js";

export type UseOffboardCancelFlowOptions = ShowCancelFlowOptions &
  Partial<InitOptions>;

/**
 * Returns a stable callback that opens the cancel flow. The latest options are read at click
 * time (via a ref) so callbacks like `onAccept` never go stale, while the returned function
 * identity stays constant across renders.
 */
export function useOffboardCancelFlow(
  options: UseOffboardCancelFlowOptions,
): () => void {
  const latest = useRef(options);
  latest.current = options;

  return useCallback(() => {
    const { publicKey, apiBaseUrl, ...flow } = latest.current;
    if (publicKey) {
      Offboard.init({ publicKey, ...(apiBaseUrl ? { apiBaseUrl } : {}) });
    }
    Offboard.showCancelFlow(flow);
  }, []);
}
