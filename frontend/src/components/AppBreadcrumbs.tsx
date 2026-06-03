import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";
import Box from "@mui/material/Box";
import { Breadcrumbs } from "@nvidia/foundations-react-core";
import { pathToRoute, rackIdFromPath, RACKS_PATH } from "../routes";

type Crumb = string | { children: ReactNode };

/**
 * Global breadcrumb bar (rendered by AppLayout under the toolbar) that shows
 * the current location as `Domain / View [/ Rack]`, derived from the URL.
 * Renders nothing for single-level destinations (e.g. Settings) per the KUI
 * guidance against breadcrumbs with no parent to navigate back to.
 */
export function AppBreadcrumbs() {
  const location = useLocation();
  const route = pathToRoute(location.pathname);

  if (!route || route.viewId === "settings") return null;

  const rackId = rackIdFromPath(location.pathname);

  const items: Crumb[] = [route.domain];
  if (rackId) {
    items.push({ children: <a href={`#${RACKS_PATH}`}>{route.label}</a> });
    items.push(rackId);
  } else {
    items.push(route.label);
  }

  return (
    <Box
      sx={{
        px: 3,
        py: 1,
        display: "flex",
        alignItems: "center",
        minHeight: 40,
        bgcolor: "background.paper",
        borderBottom: 1,
        borderColor: "divider",
      }}
    >
      <Breadcrumbs items={items} size="small" />
    </Box>
  );
}
