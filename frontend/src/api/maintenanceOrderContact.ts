/** WBDD header contact data. Restricted responses always contain null fields. */
export interface MaintenanceOrderContact {
  contact_info_state: "visible" | "restricted";
  receiver_address: string | null;
  receiver: string | null;
  /** Text throughout: leading zeros, country codes and extensions are significant. */
  receiver_phone: string | null;
}
