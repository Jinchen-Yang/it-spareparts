import { useEffect, useRef, useState } from "react";
import { Button, Modal, Radio, Space, Typography } from "antd";
import type { Offer } from "../../api/presales";
import { OfferBadge } from "./shared";

export interface ExistingOfferConflict {
  code: string;
  message?: string;
  line_id: number;
  draft_offer_id: number;
  existing_offers: Offer[];
}

export interface ExistingOfferChoice {
  on_existing: "update" | "new";
  existing_offer_id?: number;
}

export function existingOfferConflict(error: unknown): ExistingOfferConflict | undefined {
  const response = (error as { response?: { status?: number; data?: { detail?: ExistingOfferConflict } } })?.response;
  const detail = response?.data?.detail;
  return response?.status === 409 && detail?.code === "existing_offer" && Array.isArray(detail.existing_offers)
    && detail.existing_offers.length ? detail : undefined;
}

/** 只处理重复报价的业务冲突；版本冲突仍交给页面的刷新逻辑。 */
export function useExistingOfferChoice() {
  const [conflict, setConflict] = useState<ExistingOfferConflict>();
  const [offerId, setOfferId] = useState<number>();
  const resolve = useRef<(choice?: ExistingOfferChoice) => void>();
  useEffect(() => () => { resolve.current?.(); }, []);
  const finish = (choice?: ExistingOfferChoice) => {
    setConflict(undefined);
    resolve.current?.(choice);
    resolve.current = undefined;
  };
  const choose = (detail: ExistingOfferConflict) => new Promise<ExistingOfferChoice | undefined>((done) => {
    resolve.current = done;
    setOfferId(detail.existing_offers[0]?.id);
    setConflict(detail);
  });
  const dialog = <Modal title="你已有该 PN 的有效报价" open={!!conflict} onCancel={() => finish()} footer={<Space>
    <Button aria-label="取消" onClick={() => finish()}>取消</Button>
    <Button onClick={() => finish({ on_existing: "new" })}>另报一条</Button>
    <Button type="primary" disabled={!offerId} onClick={() => finish({ on_existing: "update", existing_offer_id: offerId })}>更新那条</Button>
  </Space>}>
    <Typography.Paragraph>选择要更新的报价，或另报一条；更新后价格、货源和可见范围按本次内容覆盖。</Typography.Paragraph>
    <Radio.Group value={offerId} onChange={(event) => setOfferId(event.target.value)}>
      <Space direction="vertical">{conflict?.existing_offers.map((offer) => <Radio key={offer.id} value={offer.id}>
        报价 #{offer.id} <OfferBadge offer={offer} />
      </Radio>)}</Space>
    </Radio.Group>
  </Modal>;
  return { choose, dialog };
}
