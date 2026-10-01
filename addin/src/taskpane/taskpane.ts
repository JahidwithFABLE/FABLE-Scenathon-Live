import "./taskpane.css";

/* ============================================================
   FABLE SCENATHON TRADE ADJUSTMENT

   Phase 3:
   Share your SCENATHON report + inject adjusted trade

   This version:
   ------------------------------------------------------------
   - Reads the current FABLE-C workbook
   - Reads only the required SCENATHON fields
   - Detects the selected pathway
   - Collects the user's display name
   - Builds the minimal backend payload
   - Sends the report to the FABLE backend
   - Shows a user-friendly preview
   - Does NOT upload the whole workbook
   - Modifies the workbook only when the user clicks Inject adjusted trade
   ============================================================ */

/* ============================================================
   TYPES
   ============================================================ */

interface TradeRow {
    Location: string;
    Product: string;
    Year: number;
    Import_quantity: number;
    Export_quantity: number;
}

interface ShareMetadata {
    user_name: string;
    pathway: string;
    schema_version: string;
}

interface SharePayload {
    metadata: ShareMetadata;
    trade: TradeRow[];
}

interface SubmissionResponse {
    submission_id: string;
    iteration_id: string;
    version: number;
    username: string;
    country_id: string;
    location: string;
    pathway: string;
    row_count: number;
    report_hash: string;
    status: string;
    submitted_at: string;
    duplicate: boolean;
}

interface InjectionRow {
    Pathway: string;
    ProductKey: string;
    Year: number;
    ImportsAdj: number;
    ExportsAdj: number;
}

interface InjectionPayload {
    engine_version: string;
    run_id: string;
    iteration_id: string;
    country_id: string;
    row_count: number;
    rows: InjectionRow[];
}

interface WorkbookInjectionIdentity {
    location: string;
    selectedPathway: string;
}

interface InjectionSummary {
    runId: string;
    iterationId: string;
    countryId: string;
    location: string;
    rowsWritten: number;
    selectedPathway: string;
    verifiedRows: number;
}

/* ============================================================
   CONSTANTS
   ============================================================ */

const REPORT_TABLE_CANDIDATES = [
    "REPORTING_BYPRODUCT",
    "SCENATHON_report"
];

const PATHWAYS_TABLE_CANDIDATES = [
    "PathwaysSelection"
];

const FULL_TRADE_ADJ_TABLE_CANDIDATES = [
    "FullDataTradeAdj"
];

const FINAL_TRADE_ADJ_TABLE_CANDIDATES = [
    "FinalTradeAdj"
];

const USER_NAME_STORAGE_KEY =
    "fableScenathonUserName";

const SCHEMA_VERSION =
    "1.0";

const BACKEND_BASE_URL =
    "http://127.0.0.1:8000";

const BACKEND_TIMEOUT_MS =
    30000;

/* ============================================================
   OFFICE INITIALIZATION
   ============================================================ */

Office.onReady((info) => {
    if (
        info.host !==
        Office.HostType.Excel
    ) {
        setStatus(
            "This add-in must be opened in Microsoft Excel.",
            "error"
        );

        return;
    }

    const button =
        getElement<HTMLButtonElement>(
            "shareButton"
        );

    const injectButton =
        getElement<HTMLButtonElement>(
            "injectButton"
        );

    const userNameInput =
        getElement<HTMLInputElement>(
            "userName"
        );

    const savedUserName =
        loadSavedUserName();

    if (savedUserName !== "") {
        userNameInput.value =
            savedUserName;
    }

    userNameInput.addEventListener(
        "change",
        () => {
            saveUserName(
                userNameInput.value
            );
        }
    );

    userNameInput.addEventListener(
        "blur",
        () => {
            saveUserName(
                userNameInput.value
            );
        }
    );

    button.disabled = false;
    injectButton.disabled = false;

    button.addEventListener(
        "click",
        shareScenathonReport
    );

    injectButton.addEventListener(
        "click",
        injectAdjustedTrade
    );

    setStatus(
        "Ready.",
        "success"
    );
});

/* ============================================================
   MAIN ACTION
   ============================================================ */

async function shareScenathonReport(): Promise<void> {
    const button =
        getElement<HTMLButtonElement>(
            "shareButton"
        );

    const userNameInput =
        getElement<HTMLInputElement>(
            "userName"
        );

    const userName =
        userNameInput.value.trim();

    if (userName === "") {
        setStatus(
            "Please enter your name before sharing the SCENATHON report.",
            "error"
        );

        userNameInput.focus();
        return;
    }

    saveUserName(
        userName
    );

    setActionButtonsDisabled(true);
    hideResult();
    hideInjectionResult();

    setStatus(
        "Reading SCENATHON report from the current workbook...",
        "working"
    );

    try {
        const payload =
            await buildSharePayload(
                userName
            );

        console.log(
            "FABLE SCENATHON PAYLOAD:",
            payload
        );

        setStatus(
            "Sharing SCENATHON report with the FABLE backend...",
            "working"
        );

        const submission =
            await submitSharePayload(
                payload
            );

        console.log(
            "FABLE BACKEND SUBMISSION:",
            submission
        );

        showResult(
            payload
        );

        if (submission.duplicate) {
            setStatus(
                `This report was already shared. Submission ${submission.submission_id}.`,
                "success"
            );
        }
        else {
            setStatus(
                `SCENATHON report shared successfully. Submission ${submission.submission_id}.`,
                "success"
            );
        }
    }
    catch (error) {
        console.error(
            "FABLE Add-in error:",
            error
        );

        const message =
            error instanceof Error
                ? error.message
                : String(error);

        setStatus(
            message,
            "error"
        );
    }
    finally {
        setActionButtonsDisabled(false);
    }
}

/* ============================================================
   INJECT ADJUSTED TRADE
   ============================================================ */

async function injectAdjustedTrade(): Promise<void> {
    setActionButtonsDisabled(true);
    hideInjectionResult();

    setStatus(
        "Identifying this FABLE-C workbook...",
        "working"
    );

    try {
        const identity =
            await readWorkbookInjectionIdentity();

        setStatus(
            `Looking for the latest completed trade adjustment for ${identity.location}...`,
            "working"
        );

        const payload =
            await fetchLatestInjectionPayload(
                identity.location
            );

        setStatus(
            `Run ${payload.run_id} found. Validating workbook before writing...`,
            "working"
        );

        const summary =
            await applyInjectionPayload(
                payload,
                identity
            );

        showInjectionResult(
            summary
        );

        setStatus(
            `Adjusted trade injected and verified successfully. Run ${summary.runId}.`,
            "success"
        );
    }
    catch (error) {
        console.error(
            "FABLE injection error:",
            error
        );

        const message =
            error instanceof Error
                ? error.message
                : String(error);

        setStatus(
            message,
            "error"
        );
    }
    finally {
        setActionButtonsDisabled(false);
    }
}

async function readWorkbookInjectionIdentity():
    Promise<WorkbookInjectionIdentity> {
    return Excel.run(
        async (context) => {
            const reportTable =
                await findWorkbookTable(
                    context,
                    REPORT_TABLE_CANDIDATES
                );

            const pathwaysTable =
                await findWorkbookTable(
                    context,
                    PATHWAYS_TABLE_CANDIDATES
                );

            const location =
                await readUniqueWorkbookLocation(
                    context,
                    reportTable
                );

            const selectedPathway =
                await readSelectedPathway(
                    context,
                    pathwaysTable
                );

            return {
                location,
                selectedPathway
            };
        }
    );
}

async function readUniqueWorkbookLocation(
    context: Excel.RequestContext,
    table: Excel.Table
): Promise<string> {
    const columns = table.columns;
    columns.load("items/name");
    await context.sync();

    const locationColumn =
        findRequiredColumn(
            columns.items,
            "Location"
        );

    const range =
        locationColumn.getDataBodyRange();

    range.load("values,rowCount");
    await context.sync();

    const locations =
        new Set<string>();

    for (let i = 0; i < range.rowCount; i++) {
        const value =
            String(range.values[i][0] ?? "").trim();

        if (value !== "") {
            locations.add(value);
        }
    }

    if (locations.size === 0) {
        throw new Error(
            "No Location value was found in the SCENATHON report."
        );
    }

    if (locations.size > 1) {
        throw new Error(
            `The SCENATHON report contains multiple Location values: ${Array.from(locations).join(", ")}.`
        );
    }

    return Array.from(locations)[0];
}

async function fetchLatestInjectionPayload(
    location: string
): Promise<InjectionPayload> {
    const controller =
        new AbortController();

    const timeoutId =
        window.setTimeout(
            () => controller.abort(),
            BACKEND_TIMEOUT_MS
        );

    try {
        const url =
            `${BACKEND_BASE_URL}/api/v1/injection/latest?location=${encodeURIComponent(location)}`;

        const response =
            await fetch(
                url,
                {
                    method: "GET",
                    signal: controller.signal
                }
            );

        let responseBody: unknown = null;

        try {
            responseBody =
                await response.json();
        }
        catch {
            responseBody = null;
        }

        if (!response.ok) {
            throw new Error(
                readBackendError(
                    response.status,
                    responseBody
                )
            );
        }

        if (
            !responseBody ||
            typeof responseBody !== "object" ||
            !("run_id" in responseBody) ||
            !("iteration_id" in responseBody) ||
            !("country_id" in responseBody) ||
            !("row_count" in responseBody) ||
            !("rows" in responseBody) ||
            !Array.isArray(
                (responseBody as { rows?: unknown }).rows
            )
        ) {
            throw new Error(
                "The backend returned an unexpected adjusted-trade response."
            );
        }

        return responseBody as InjectionPayload;
    }
    catch (error) {
        if (
            error instanceof DOMException &&
            error.name === "AbortError"
        ) {
            throw new Error(
                "The backend did not respond within 30 seconds."
            );
        }

        throw error;
    }
    finally {
        window.clearTimeout(
            timeoutId
        );
    }
}

function readBackendError(
    status: number,
    responseBody: unknown
): string {
    let message =
        `Backend request failed (${status}).`;

    if (
        responseBody &&
        typeof responseBody === "object" &&
        "detail" in responseBody
    ) {
        const detail =
            (responseBody as { detail?: unknown }).detail;

        if (typeof detail === "string") {
            message = detail;
        }
        else if (detail !== undefined) {
            message = JSON.stringify(detail);
        }
    }

    return message;
}

async function applyInjectionPayload(
    payload: InjectionPayload,
    identity: WorkbookInjectionIdentity
): Promise<InjectionSummary> {
    validateInjectionPayload(payload);

    return Excel.run(
        async (context) => {
            const fullTradeTable =
                await findWorkbookTable(
                    context,
                    FULL_TRADE_ADJ_TABLE_CANDIDATES
                );

            const pathwaysTable =
                await findWorkbookTable(
                    context,
                    PATHWAYS_TABLE_CANDIDATES
                );

            const finalTradeTable =
                await findWorkbookTable(
                    context,
                    FINAL_TRADE_ADJ_TABLE_CANDIDATES
                );

            const selectedPathway =
                await readSelectedPathway(
                    context,
                    pathwaysTable
                );

            if (
                normalizePathwayKey(selectedPathway) !==
                normalizePathwayKey(identity.selectedPathway)
            ) {
                throw new Error(
                    "The selected pathway changed while the adjusted trade was being prepared. Please try again."
                );
            }

            const writeResult =
                await writeFullDataTradeAdj(
                    context,
                    fullTradeTable,
                    payload
                );

            await setSelectedPathwayS12Yes(
                context,
                pathwaysTable,
                selectedPathway
            );

            context.workbook.application.calculate(
                Excel.CalculationType.full
            );
            await context.sync();

            const verifiedRows =
                await verifyFinalTradeAdj(
                    context,
                    finalTradeTable,
                    payload,
                    selectedPathway
                );

            return {
                runId: payload.run_id,
                iterationId: payload.iteration_id,
                countryId: payload.country_id,
                location: identity.location,
                rowsWritten: writeResult.rowsWritten,
                selectedPathway,
                verifiedRows
            };
        }
    );
}

function validateInjectionPayload(
    payload: InjectionPayload
): void {
    if (payload.rows.length === 0) {
        throw new Error(
            "The backend returned an empty adjusted-trade payload."
        );
    }

    if (payload.row_count !== payload.rows.length) {
        throw new Error(
            `Adjusted-trade row count mismatch: backend metadata says ${payload.row_count}, but ${payload.rows.length} rows were received.`
        );
    }

    const keys = new Set<string>();

    for (let i = 0; i < payload.rows.length; i++) {
        const row = payload.rows[i];
        const year = normalizeTradeYear(
            row.Year,
            `backend row ${i + 1}`
        );

        if (
            !Number.isFinite(row.ImportsAdj) ||
            !Number.isFinite(row.ExportsAdj)
        ) {
            throw new Error(
                `Adjusted trade contains a non-numeric value at backend row ${i + 1}.`
            );
        }

        const key = makeTradeKey(
            row.Pathway,
            row.ProductKey,
            year
        );

        if (keys.has(key)) {
            throw new Error(
                `The backend adjusted-trade payload contains a duplicate key at row ${i + 1}.`
            );
        }

        keys.add(key);
    }
}

async function writeFullDataTradeAdj(
    context: Excel.RequestContext,
    table: Excel.Table,
    payload: InjectionPayload
): Promise<{ rowsWritten: number }> {
    const columns = table.columns;
    columns.load("items/name");
    await context.sync();

    const pathwayColumn = findRequiredColumn(columns.items, "Pathway");
    const productColumn = findRequiredColumn(columns.items, "Product");
    const yearColumn = findRequiredColumn(columns.items, "Year");
    const importsColumn = findRequiredColumn(columns.items, "ImportsAdj");
    const exportsColumn = findRequiredColumn(columns.items, "ExportsAdj");

    const pathwayRange = pathwayColumn.getDataBodyRange();
    const productRange = productColumn.getDataBodyRange();
    const yearRange = yearColumn.getDataBodyRange();
    const importsRange = importsColumn.getDataBodyRange();
    const exportsRange = exportsColumn.getDataBodyRange();

    pathwayRange.load("values,rowCount");
    productRange.load("values,rowCount");
    yearRange.load("values,rowCount");
    importsRange.load("values,rowCount");
    exportsRange.load("values,rowCount");
    await context.sync();

    const counts = [
        pathwayRange.rowCount,
        productRange.rowCount,
        yearRange.rowCount,
        importsRange.rowCount,
        exportsRange.rowCount
    ];

    if (new Set(counts).size !== 1) {
        throw new Error(
            "FullDataTradeAdj columns have inconsistent row counts."
        );
    }

    if (pathwayRange.rowCount !== payload.rows.length) {
        throw new Error(
            `FullDataTradeAdj has ${pathwayRange.rowCount} rows, but the backend returned ${payload.rows.length}. Nothing was written.`
        );
    }

    const backendByKey =
        new Map<string, InjectionRow>();

    for (const row of payload.rows) {
        backendByKey.set(
            makeTradeKey(
                row.Pathway,
                row.ProductKey,
                normalizeTradeYear(row.Year, "backend row")
            ),
            row
        );
    }

    const targetKeys =
        new Set<string>();
    const importValues: number[][] = [];
    const exportValues: number[][] = [];

    for (let i = 0; i < pathwayRange.rowCount; i++) {
        const pathway = String(pathwayRange.values[i][0] ?? "").trim();
        const product = String(productRange.values[i][0] ?? "").trim();
        const year = normalizeTradeYear(
            yearRange.values[i][0],
            `FullDataTradeAdj row ${i + 1}`
        );

        if (pathway === "" || product === "") {
            throw new Error(
                `FullDataTradeAdj has a blank Pathway or Product at row ${i + 1}. Nothing was written.`
            );
        }

        const key = makeTradeKey(pathway, product, year);

        if (targetKeys.has(key)) {
            throw new Error(
                `FullDataTradeAdj contains a duplicate Pathway/Product/Year key at row ${i + 1}. Nothing was written.`
            );
        }

        targetKeys.add(key);

        const adjusted = backendByKey.get(key);
        if (!adjusted) {
            throw new Error(
                `No backend adjusted value matches FullDataTradeAdj row ${i + 1} (${pathway} / ${product} / ${year}). Nothing was written.`
            );
        }

        importValues.push([adjusted.ImportsAdj]);
        exportValues.push([adjusted.ExportsAdj]);
    }

    for (const key of backendByKey.keys()) {
        if (!targetKeys.has(key)) {
            throw new Error(
                "The backend contains a Pathway/Product/Year key that does not exist in FullDataTradeAdj. Nothing was written."
            );
        }
    }

    importsRange.values = importValues;
    exportsRange.values = exportValues;
    await context.sync();

    return {
        rowsWritten: pathwayRange.rowCount
    };
}

async function setSelectedPathwayS12Yes(
    context: Excel.RequestContext,
    table: Excel.Table,
    selectedPathway: string
): Promise<void> {
    const columns = table.columns;
    columns.load("items/name");
    await context.sync();

    const selectionColumn = findRequiredColumn(columns.items, "SELECTION");
    const pathwayColumn = findRequiredColumn(columns.items, "PATHWAY");
    const s12Column = findRequiredColumn(columns.items, "S12");

    const selectionRange = selectionColumn.getDataBodyRange();
    const pathwayRange = pathwayColumn.getDataBodyRange();
    const s12Range = s12Column.getDataBodyRange();

    selectionRange.load("values,rowCount");
    pathwayRange.load("values,rowCount");
    s12Range.load("rowCount");
    await context.sync();

    if (
        selectionRange.rowCount !== pathwayRange.rowCount ||
        selectionRange.rowCount !== s12Range.rowCount
    ) {
        throw new Error(
            "PathwaysSelection SELECTION, PATHWAY and S12 columns have inconsistent row counts."
        );
    }

    const selectedRows: number[] = [];

    for (let i = 0; i < selectionRange.rowCount; i++) {
        const marker =
            String(selectionRange.values[i][0] ?? "")
                .trim()
                .toLowerCase();

        if (marker === "x") {
            selectedRows.push(i);
        }
    }

    if (selectedRows.length !== 1) {
        throw new Error(
            `Expected exactly one selected pathway before setting S12, found ${selectedRows.length}.`
        );
    }

    const selectedIndex = selectedRows[0];
    const workbookPathway =
        String(pathwayRange.values[selectedIndex][0] ?? "").trim();

    if (
        normalizePathwayKey(workbookPathway) !==
        normalizePathwayKey(selectedPathway)
    ) {
        throw new Error(
            "The selected pathway changed before S12 could be enabled."
        );
    }

    const s12Cell =
        s12Range.getCell(
            selectedIndex,
            0
        );

    s12Cell.values = [["Yes"]];
    await context.sync();
}

async function verifyFinalTradeAdj(
    context: Excel.RequestContext,
    table: Excel.Table,
    payload: InjectionPayload,
    selectedPathway: string
): Promise<number> {
    const columns = table.columns;
    columns.load("items/name");
    await context.sync();

    const pathwayColumn = findRequiredColumn(columns.items, "Pathway");
    const productColumn = findRequiredColumn(columns.items, "Product");
    const yearColumn = findRequiredColumn(columns.items, "Year");
    const importsColumn = findRequiredColumn(columns.items, "ImportsAdj");
    const exportsColumn = findRequiredColumn(columns.items, "ExportsAdj");

    const pathwayRange = pathwayColumn.getDataBodyRange();
    const productRange = productColumn.getDataBodyRange();
    const yearRange = yearColumn.getDataBodyRange();
    const importsRange = importsColumn.getDataBodyRange();
    const exportsRange = exportsColumn.getDataBodyRange();

    pathwayRange.load("values,rowCount");
    productRange.load("values,rowCount");
    yearRange.load("values,rowCount");
    importsRange.load("values,rowCount");
    exportsRange.load("values,rowCount");
    await context.sync();

    const expected =
        new Map<string, InjectionRow>();

    for (const row of payload.rows) {
        if (
            normalizePathwayKey(row.Pathway) ===
            normalizePathwayKey(selectedPathway)
        ) {
            expected.set(
                makeTradeKey(
                    row.Pathway,
                    row.ProductKey,
                    normalizeTradeYear(row.Year, "backend verification row")
                ),
                row
            );
        }
    }

    if (expected.size === 0) {
        throw new Error(
            `The backend payload contains no adjusted rows for the selected pathway ${selectedPathway}.`
        );
    }

    const seen = new Set<string>();
    let verified = 0;

    for (let i = 0; i < pathwayRange.rowCount; i++) {
        const pathway = String(pathwayRange.values[i][0] ?? "").trim();

        if (
            normalizePathwayKey(pathway) !==
            normalizePathwayKey(selectedPathway)
        ) {
            continue;
        }

        const product = String(productRange.values[i][0] ?? "").trim();
        const year = normalizeTradeYear(
            yearRange.values[i][0],
            `FinalTradeAdj row ${i + 1}`
        );
        const key = makeTradeKey(pathway, product, year);
        const adjusted = expected.get(key);

        if (!adjusted) {
            throw new Error(
                `FinalTradeAdj contains an unexpected selected-pathway row at ${i + 1}: ${pathway} / ${product} / ${year}.`
            );
        }

        if (seen.has(key)) {
            throw new Error(
                `FinalTradeAdj contains a duplicate selected-pathway key at row ${i + 1}.`
            );
        }
        seen.add(key);

        const actualImports = requireNumber(
            importsRange.values[i][0],
            "FinalTradeAdj ImportsAdj",
            i + 1
        );
        const actualExports = requireNumber(
            exportsRange.values[i][0],
            "FinalTradeAdj ExportsAdj",
            i + 1
        );

        if (
            !nearlyEqual(actualImports, adjusted.ImportsAdj) ||
            !nearlyEqual(actualExports, adjusted.ExportsAdj)
        ) {
            throw new Error(
                `FinalTradeAdj verification failed at row ${i + 1} (${pathway} / ${product} / ${year}).`
            );
        }

        verified++;
    }

    if (verified !== expected.size) {
        throw new Error(
            `FinalTradeAdj verification covered ${verified} rows, but ${expected.size} selected-pathway rows were expected.`
        );
    }

    return verified;
}

function normalizeProductKey(
    value: string
): string {
    return value
        .trim()
        .toLowerCase();
}

function normalizePathwayKey(
    value: string
): string {
    return value
        .trim()
        .toLowerCase();
}

function normalizeTradeYear(
    value: unknown,
    label: string
): number {
    const parsed =
        typeof value === "number"
            ? value
            : Number(String(value).trim());

    if (
        !Number.isFinite(parsed) ||
        !Number.isInteger(parsed)
    ) {
        throw new Error(
            `${label} has an invalid Year value: ${String(value)}.`
        );
    }

    return parsed;
}

function makeTradeKey(
    pathway: string,
    product: string,
    year: number
): string {
    return [
        normalizePathwayKey(pathway),
        normalizeProductKey(product),
        String(year)
    ].join("\u001f");
}

function nearlyEqual(
    actual: number,
    expected: number
): boolean {
    const tolerance =
        1e-7 *
        Math.max(
            1,
            Math.abs(expected)
        );

    return Math.abs(actual - expected) <= tolerance;
}

/* ============================================================
   BUILD PAYLOAD
   ============================================================ */

async function buildSharePayload(
    userName: string
): Promise<SharePayload> {
    return Excel.run(
        async (context) => {
            const reportTable =
                await findWorkbookTable(
                    context,
                    REPORT_TABLE_CANDIDATES
                );

            const pathwaysTable =
                await findWorkbookTable(
                    context,
                    PATHWAYS_TABLE_CANDIDATES
                );

            const pathway =
                await readSelectedPathway(
                    context,
                    pathwaysTable
                );

            const trade =
                await readTradeData(
                    context,
                    reportTable
                );

            return {
                metadata: {
                    user_name:
                        userName,
                    pathway:
                        pathway,
                    schema_version:
                        SCHEMA_VERSION
                },
                trade:
                    trade
            };
        }
    );
}

/* ============================================================
   SUBMIT PAYLOAD TO BACKEND
   ============================================================ */

async function submitSharePayload(
    payload: SharePayload
): Promise<SubmissionResponse> {
    const requestBody = {
        username:
            payload.metadata.user_name,
        pathway:
            payload.metadata.pathway,
        rows:
            payload.trade
    };

    const controller =
        new AbortController();

    const timeoutId =
        window.setTimeout(
            () => controller.abort(),
            BACKEND_TIMEOUT_MS
        );

    try {
        const response = await fetch(
            `${BACKEND_BASE_URL}/api/v1/submissions`,
            {
                method: "POST",
                headers: {
                    "Content-Type":
                        "application/json"
                },
                body: JSON.stringify(
                    requestBody
                ),
                signal:
                    controller.signal
            }
        );

        let responseBody: unknown = null;

        try {
            responseBody =
                await response.json();
        }
        catch {
            responseBody = null;
        }

        if (!response.ok) {
            let message =
                `Backend request failed (${response.status}).`;

            if (
                responseBody &&
                typeof responseBody === "object" &&
                "detail" in responseBody
            ) {
                const detail =
                    (
                        responseBody as
                        { detail?: unknown }
                    ).detail;

                if (typeof detail === "string") {
                    message = detail;
                }
                else if (detail !== undefined) {
                    message =
                        JSON.stringify(
                            detail
                        );
                }
            }

            throw new Error(
                message
            );
        }

        if (
            !responseBody ||
            typeof responseBody !== "object" ||
            !("submission_id" in responseBody) ||
            !("iteration_id" in responseBody) ||
            !("row_count" in responseBody)
        ) {
            throw new Error(
                "The backend returned an unexpected response."
            );
        }

        return responseBody as SubmissionResponse;
    }
    catch (error) {
        if (
            error instanceof DOMException &&
            error.name === "AbortError"
        ) {
            throw new Error(
                "The backend did not respond within 30 seconds."
            );
        }

        throw error;
    }
    finally {
        window.clearTimeout(
            timeoutId
        );
    }
}

/* ============================================================
   FIND WORKBOOK TABLE
   ============================================================ */

async function findWorkbookTable(
    context: Excel.RequestContext,
    candidates: string[]
): Promise<Excel.Table> {
    const tables =
        context.workbook.tables;

    tables.load(
        "items/name"
    );

    await context.sync();

    const wanted =
        new Set(
            candidates.map(
                normalizeIdentifier
            )
        );

    const match =
        tables.items.find(
            table =>
                wanted.has(
                    normalizeIdentifier(
                        table.name
                    )
                )
        );

    if (!match) {
        const available =
            tables.items
                .map(
                    table => table.name
                )
                .sort()
                .join(", ");

        throw new Error(
            `Required Excel table was not found. ` +
            `Expected one of: ${candidates.join(", ")}. ` +
            `Tables found: ${available}`
        );
    }

    return match;
}

/* ============================================================
   READ TRADE DATA
   ============================================================ */

async function readTradeData(
    context: Excel.RequestContext,
    table: Excel.Table
): Promise<TradeRow[]> {
    const columns =
        table.columns;

    columns.load(
        "items/name"
    );

    await context.sync();

    const locationColumn =
        findRequiredColumn(
            columns.items,
            "Location"
        );

    const productColumn =
        findRequiredColumn(
            columns.items,
            "Product"
        );

    const yearColumn =
        findRequiredColumn(
            columns.items,
            "Year"
        );

    const importColumn =
        findRequiredColumn(
            columns.items,
            "Import_quantity"
        );

    const exportColumn =
        findRequiredColumn(
            columns.items,
            "Export_quantity"
        );

    const locationRange =
        locationColumn
            .getDataBodyRange();

    const productRange =
        productColumn
            .getDataBodyRange();

    const yearRange =
        yearColumn
            .getDataBodyRange();

    const importRange =
        importColumn
            .getDataBodyRange();

    const exportRange =
        exportColumn
            .getDataBodyRange();

    locationRange.load(
        "values,rowCount"
    );

    productRange.load(
        "values,rowCount"
    );

    yearRange.load(
        "values,rowCount"
    );

    importRange.load(
        "values,rowCount"
    );

    exportRange.load(
        "values,rowCount"
    );

    await context.sync();

    const counts = [
        locationRange.rowCount,
        productRange.rowCount,
        yearRange.rowCount,
        importRange.rowCount,
        exportRange.rowCount
    ];

    const uniqueCounts =
        new Set(
            counts
        );

    if (uniqueCounts.size !== 1) {
        throw new Error(
            "The required SCENATHON columns have inconsistent row counts."
        );
    }

    const trade: TradeRow[] = [];

    for (
        let i = 0;
        i < locationRange.rowCount;
        i++
    ) {
        const rowNumber =
            i + 1;

        const rawLocation =
            locationRange.values[i][0];

        const rawProduct =
            productRange.values[i][0];

        const rawYear =
            yearRange.values[i][0];

        const rawImport =
            importRange.values[i][0];

        const rawExport =
            exportRange.values[i][0];

        if (
            isBlank(rawLocation) &&
            isBlank(rawProduct) &&
            isBlank(rawYear) &&
            isBlank(rawImport) &&
            isBlank(rawExport)
        ) {
            continue;
        }

        const Location =
            requireText(
                rawLocation,
                "Location",
                rowNumber,
                false
            );

        /*
         * Preserve Product exactly as stored in the workbook.
         * No semantic normalization is performed here.
         * Example: lentil != lentils.
         */
        const Product =
            requireText(
                rawProduct,
                "Product",
                rowNumber,
                true
            );

        const Year =
            requireNumber(
                rawYear,
                "Year",
                rowNumber
            );

        const Import_quantity =
            requireNumber(
                rawImport,
                "Import_quantity",
                rowNumber
            );

        const Export_quantity =
            requireNumber(
                rawExport,
                "Export_quantity",
                rowNumber
            );

        trade.push({
            Location,
            Product,
            Year,
            Import_quantity,
            Export_quantity
        });
    }

    if (trade.length === 0) {
        throw new Error(
            "No usable SCENATHON trade rows were found."
        );
    }

    return trade;
}

/* ============================================================
   READ SELECTED PATHWAY
   ============================================================ */

async function readSelectedPathway(
    context: Excel.RequestContext,
    table: Excel.Table
): Promise<string> {
    const columns =
        table.columns;

    columns.load(
        "items/name"
    );

    await context.sync();

    const selectionColumn =
        findRequiredColumn(
            columns.items,
            "SELECTION"
        );

    const pathwayColumn =
        findRequiredColumn(
            columns.items,
            "PATHWAY"
        );

    const selectionRange =
        selectionColumn
            .getDataBodyRange();

    const pathwayRange =
        pathwayColumn
            .getDataBodyRange();

    selectionRange.load(
        "values,rowCount"
    );

    pathwayRange.load(
        "values,rowCount"
    );

    await context.sync();

    if (
        selectionRange.rowCount !==
        pathwayRange.rowCount
    ) {
        throw new Error(
            "PathwaysSelection SELECTION and PATHWAY columns have inconsistent row counts."
        );
    }

    const selectedRows: number[] = [];

    for (
        let i = 0;
        i < selectionRange.rowCount;
        i++
    ) {
        const marker =
            String(
                selectionRange.values[i][0] ?? ""
            )
                .trim()
                .toLowerCase();

        if (marker === "x") {
            selectedRows.push(
                i
            );
        }
    }

    if (selectedRows.length === 0) {
        throw new Error(
            'No pathway is selected. Expected "x" in PathwaysSelection[SELECTION].'
        );
    }

    if (selectedRows.length > 1) {
        throw new Error(
            `Multiple pathways are selected (${selectedRows.length}). ` +
            `Exactly one row must contain "x" in PathwaysSelection[SELECTION].`
        );
    }

    const selectedIndex =
        selectedRows[0];

    const pathway =
        String(
            pathwayRange.values[selectedIndex][0] ?? ""
        )
            .trim();

    if (pathway === "") {
        throw new Error(
            "The selected pathway has an empty PATHWAY value."
        );
    }

    return pathway;
}

/* ============================================================
   FIND REQUIRED COLUMN
   ============================================================ */

function findRequiredColumn(
    columns: Excel.TableColumn[],
    requiredName: string
): Excel.TableColumn {
    const wanted =
        normalizeIdentifier(
            requiredName
        );

    const match =
        columns.find(
            column =>
                normalizeIdentifier(
                    column.name
                ) === wanted
        );

    if (!match) {
        const available =
            columns
                .map(
                    column => column.name
                )
                .join(", ");

        throw new Error(
            `Required column "${requiredName}" was not found. ` +
            `Available columns: ${available}`
        );
    }

    return match;
}

/* ============================================================
   VALUE VALIDATION
   ============================================================ */

function requireText(
    value: unknown,
    fieldName: string,
    rowNumber: number,
    preserveExactly: boolean
): string {
    if (
        value === null ||
        value === undefined
    ) {
        throw new Error(
            `${fieldName} is empty at SCENATHON row ${rowNumber}.`
        );
    }

    const original =
        String(
            value
        );

    if (original.trim() === "") {
        throw new Error(
            `${fieldName} is empty at SCENATHON row ${rowNumber}.`
        );
    }

    if (preserveExactly) {
        return original;
    }

    return original.trim();
}

/* ============================================================
   NUMBER VALIDATION
   ============================================================ */

function requireNumber(
    value: unknown,
    fieldName: string,
    rowNumber: number
): number {
    if (
        value === null ||
        value === undefined ||
        String(value).trim() === ""
    ) {
        throw new Error(
            `${fieldName} is empty at SCENATHON row ${rowNumber}.`
        );
    }

    if (
        typeof value === "number" &&
        Number.isFinite(value)
    ) {
        return value;
    }

    const parsed =
        Number(
            String(value).trim()
        );

    if (!Number.isFinite(parsed)) {
        throw new Error(
            `${fieldName} is not numeric at SCENATHON row ` +
            `${rowNumber}: "${String(value)}"`
        );
    }

    return parsed;
}

/* ============================================================
   BLANK CHECK
   ============================================================ */

function isBlank(
    value: unknown
): boolean {
    return (
        value === null ||
        value === undefined ||
        String(value).trim() === ""
    );
}

/* ============================================================
   IDENTIFIER NORMALIZATION
   ============================================================ */

function normalizeIdentifier(
    value: string
): string {
    return value
        .trim()
        .toLowerCase()
        .replace(
            /[\s_-]+/g,
            ""
        );
}

/* ============================================================
   RESULT UI
   ============================================================ */

function showResult(
    payload: SharePayload
): void {
    const result =
        getElement<HTMLElement>(
            "result"
        );

    const summaryUser =
        getElement<HTMLElement>(
            "summaryUser"
        );

    const summaryPathway =
        getElement<HTMLElement>(
            "summaryPathway"
        );

    const summaryRows =
        getElement<HTMLElement>(
            "summaryRows"
        );

    summaryUser.textContent =
        payload.metadata.user_name;

    summaryPathway.textContent =
        payload.metadata.pathway;

    summaryRows.textContent =
        payload.trade.length
            .toLocaleString();

    renderTradePreview(
        payload.trade
    );

    result.hidden = false;
}

/* ============================================================
   TRADE TABLE PREVIEW
   ============================================================ */

function renderTradePreview(
    trade: TradeRow[]
): void {
    const body =
        getElement<HTMLTableSectionElement>(
            "previewBody"
        );

    const previewCount =
        getElement<HTMLElement>(
            "previewCount"
        );

    body.replaceChildren();

    const previewRows =
        trade.slice(
            0,
            5
        );

    for (const row of previewRows) {
        const tr =
            document.createElement(
                "tr"
            );

        appendCell(
            tr,
            row.Location
        );

        appendCell(
            tr,
            row.Product
        );

        appendCell(
            tr,
            String(row.Year),
            "number-cell"
        );

        appendCell(
            tr,
            formatNumber(
                row.Import_quantity
            ),
            "number-cell"
        );

        appendCell(
            tr,
            formatNumber(
                row.Export_quantity
            ),
            "number-cell"
        );

        body.appendChild(
            tr
        );
    }

    previewCount.textContent =
        `Showing ${previewRows.length.toLocaleString()} ` +
        `of ${trade.length.toLocaleString()} rows`;
}

/* ============================================================
   TABLE CELL HELPER
   ============================================================ */

function appendCell(
    row: HTMLTableRowElement,
    value: string,
    className?: string
): void {
    const cell =
        document.createElement(
            "td"
        );

    cell.textContent =
        value;

    if (className) {
        cell.className =
            className;
    }

    row.appendChild(
        cell
    );
}

/* ============================================================
   NUMBER DISPLAY
   ============================================================ */

function formatNumber(
    value: number
): string {
    return value.toLocaleString(
        undefined,
        {
            maximumFractionDigits: 6
        }
    );
}

/* ============================================================
   HIDE RESULT
   ============================================================ */

function hideResult(): void {
    const result =
        getElement<HTMLElement>(
            "result"
        );

    result.hidden = true;
}

/* ============================================================
   STATUS
   ============================================================ */

function setStatus(
    message: string,
    type:
        | "success"
        | "error"
        | "working"
): void {
    const status =
        getElement<HTMLElement>(
            "status"
        );

    status.textContent =
        message;

    status.className =
        `status ${type}`;
}

/* ============================================================
   ACTION BUTTONS + INJECTION RESULT
   ============================================================ */

function setActionButtonsDisabled(
    disabled: boolean
): void {
    getElement<HTMLButtonElement>(
        "shareButton"
    ).disabled = disabled;

    getElement<HTMLButtonElement>(
        "injectButton"
    ).disabled = disabled;
}

function showInjectionResult(
    summary: InjectionSummary
): void {
    getElement<HTMLElement>("injectRunId").textContent = summary.runId;
    getElement<HTMLElement>("injectIteration").textContent = summary.iterationId;
    getElement<HTMLElement>("injectCountry").textContent =
        `${summary.location} (${summary.countryId})`;
    getElement<HTMLElement>("injectRows").textContent =
        summary.rowsWritten.toLocaleString();
    getElement<HTMLElement>("injectPathway").textContent = summary.selectedPathway;
    getElement<HTMLElement>("injectVerified").textContent =
        summary.verifiedRows.toLocaleString();

    getElement<HTMLElement>(
        "injectionResult"
    ).hidden = false;
}

function hideInjectionResult(): void {
    getElement<HTMLElement>(
        "injectionResult"
    ).hidden = true;
}

/* ============================================================
   USER NAME STORAGE
   ============================================================ */

function saveUserName(
    value: string
): void {
    const cleanValue =
        value.trim();

    if (cleanValue === "") {
        return;
    }

    try {
        window.localStorage.setItem(
            USER_NAME_STORAGE_KEY,
            cleanValue
        );
    }
    catch (error) {
        console.warn(
            "Could not save FABLE user name locally.",
            error
        );
    }
}

function loadSavedUserName(): string {
    try {
        return (
            window.localStorage.getItem(
                USER_NAME_STORAGE_KEY
            ) ?? ""
        );
    }
    catch (error) {
        console.warn(
            "Could not load saved FABLE user name.",
            error
        );

        return "";
    }
}

/* ============================================================
   DOM HELPER
   ============================================================ */

function getElement<T extends HTMLElement>(
    id: string
): T {
    const element =
        document.getElementById(
            id
        );

    if (!element) {
        throw new Error(
            `UI element "${id}" was not found.`
        );
    }

    return element as T;
}
