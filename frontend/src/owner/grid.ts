// AG Grid for the Week and Books tabs only, so the landing and Today load without it.
import {
  CellStyleModule, ClientSideRowModelModule, CsvExportModule, ModuleRegistry, PaginationModule, QuickFilterModule,
  TooltipModule, themeQuartz,
} from "ag-grid-community";

/** Only what the console's two grids use: rows, paging, search, CSV, tooltips and coloured cells. */
ModuleRegistry.registerModules([
  ClientSideRowModelModule, PaginationModule, QuickFilterModule, CsvExportModule, TooltipModule, CellStyleModule,
]);

/** AG Grid themed from the house tokens, one set of params per mode. */
const tokens = {
  fontFamily: "Inter, system-ui, sans-serif",
  headerFontFamily: "Inter, system-ui, sans-serif",
  fontSize: 13,
  headerFontSize: 12,
  headerFontWeight: 600,
  foregroundColor: "var(--ink)",
  textColor: "var(--ink)",
  backgroundColor: "var(--paper)",
  chromeBackgroundColor: "var(--paper)",
  headerBackgroundColor: "var(--sand)",
  headerTextColor: "var(--muted)",
  borderColor: "var(--line)",
  accentColor: "var(--clay)",
  oddRowBackgroundColor: "var(--paper)",
  wrapperBorderRadius: 14,
  borderRadius: 8,
  spacing: 7,
  cellHorizontalPadding: 12,
  rowBorder: { color: "var(--line)" },
  columnBorder: false,
  wrapperBorder: { color: "var(--line)" },
  headerRowBorder: { color: "var(--line)" },
  inputBackgroundColor: "var(--paper)",
  inputBorder: { color: "var(--line)" },
  inputFocusBorder: { color: "var(--clay)" },
  inputTextColor: "var(--ink)",
} as const;

/** Mode params match the wrapper's data-ag-theme-mode; the wrapper also carries the ag-theme-mode class. */
export const gridTheme = themeQuartz
  .withParams({ ...tokens, browserColorScheme: "inherit" })
  .withParams({ ...tokens, browserColorScheme: "light", rowHoverColor: "#f7efe3", selectedRowBackgroundColor: "#f6e3d8" }, "light")
  .withParams({ ...tokens, browserColorScheme: "dark", rowHoverColor: "#241f1a", selectedRowBackgroundColor: "#3b2519" }, "dark");
