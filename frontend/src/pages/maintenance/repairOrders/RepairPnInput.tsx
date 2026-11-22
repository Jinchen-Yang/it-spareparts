import { forwardRef } from "react";
import { Input, type InputRef } from "antd";
import LivePartSearch, { type LivePartSearchProps } from "../../../components/parts/LivePartSearch";

type RepairPnInputProps = NonNullable<LivePartSearchProps["inputProps"]> & {
  value?: string;
  onChange?: (value: string) => void;
};

// 保留 Form.Item 的普通字符串值，选中联想时写入标准 PN。
export const RepairPnInput = forwardRef<InputRef, RepairPnInputProps>(function RepairPnInput(
  { value = "", onChange, ...props }, ref,
) {
  const inputProps = { placeholder: "输入备件 PN", maxLength: 128, autoComplete: "off", ...props };
  if (inputProps.readOnly) return <Input {...inputProps} ref={ref} value={value} />;

  return <LivePartSearch inputValue={value} onInputChange={onChange} inputRef={ref}
    inputProps={{ ...inputProps, role: "combobox" }} />;
});
