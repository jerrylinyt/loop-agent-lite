/** 序號式 stale-response 守衛：多個非同步請求可能亂序返回時，只讓最後一次 begin() 標記的請求生效。 */
import { useRef } from "react";

export interface StaleGuard {
  /** 標記一次新請求；回傳的函式在請求返回後呼叫，可判斷此請求是否仍是最新的。 */
  begin: () => () => boolean;
  /** 讓所有進行中的請求作廢（例如輸入變更時重置狀態），但不啟動新請求。 */
  cancelPending: () => void;
}

export default function useStaleGuard(): StaleGuard {
  const seq = useRef(0);
  // 以 ref 保存單一 guard 物件，讓 begin/cancelPending 引用在元件生命週期內保持穩定。
  const guard = useRef<StaleGuard>({
    begin: () => {
      const token = seq.current + 1;
      seq.current = token;
      return () => token === seq.current;
    },
    cancelPending: () => { seq.current += 1; },
  });
  return guard.current;
}
