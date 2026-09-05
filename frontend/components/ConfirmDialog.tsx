"use client";

/* W8：友好确认弹窗（替代原生 confirm）。通过 useConfirm() 在任意组件内
 * `if (await confirm({ title, message })) { ... }` 使用。 */

import { useCallback, useRef, useState } from "react";

export interface ConfirmOptions {
  title: string;
  message?: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

interface ConfirmRequest extends ConfirmOptions {
  resolve: (v: boolean) => void;
}

export function useConfirm() {
  const [req, setReq] = useState<ConfirmRequest | null>(null);
  const reqRef = useRef<ConfirmRequest | null>(null);

  const confirm = useCallback((opts: ConfirmOptions): Promise<boolean> => {
    return new Promise<boolean>((resolve) => {
      const r = { ...opts, resolve };
      reqRef.current = r;
      setReq(r);
    });
  }, []);

  const settle = useCallback((v: boolean) => {
    reqRef.current?.resolve(v);
    reqRef.current = null;
    setReq(null);
  }, []);

  const dialog = req ? (
    <div className="modal show" id="confirmDialog" role="alertdialog" aria-modal="true"
      onClick={(e) => { if (e.target === e.currentTarget) settle(false); }}>
      <div className="modal-panel narrow confirm-panel">
        <div className="confirm-title">{req.title}</div>
        {req.message && <div className="confirm-message">{req.message}</div>}
        <div className="settings-actions" style={{ marginTop: 18 }}>
          <button className="actionbtn" type="button" onClick={() => settle(false)}>
            {req.cancelText || "取消"}
          </button>
          <button
            className={`actionbtn ${req.danger ? "danger" : "primary"}`}
            type="button"
            onClick={() => settle(true)}
          >
            {req.confirmText || "确定"}
          </button>
        </div>
      </div>
    </div>
  ) : null;

  return { confirm, confirmDialog: dialog };
}
