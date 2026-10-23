import { useEffect, useState } from "react";
import type { Capabilities } from "../../api/pnCatalog";
import { useCatalogAccess } from "./access";

export default function useCapabilities() {
  const { client } = useCatalogAccess();
  const [capabilities, setCapabilities] = useState<Capabilities>({ media: false, research: false, recognition: false, bundle: false });
  const [error, setError] = useState(false);
  useEffect(() => { let alive = true; setCapabilities({ media: false, research: false, recognition: false, bundle: false }); setError(false); client.get<Capabilities>("/capabilities").then(({ data }) => { if (alive) setCapabilities(data); }).catch(() => { if (alive) setError(true); }); return () => { alive = false; }; }, [client]);
  return { capabilities, configurationError: error };
}
