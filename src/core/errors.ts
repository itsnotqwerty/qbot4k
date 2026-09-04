export type ErrorDetails = Readonly<Record<string, unknown>>;

export class ApplicationError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: ErrorDetails;

  constructor(
    code: string,
    message: string,
    options: { status?: number; details?: ErrorDetails; cause?: unknown } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "ApplicationError";
    this.code = code;
    this.status = options.status ?? 500;
    this.details = Object.freeze({ ...(options.details ?? {}) });
  }

  toJSON(): Record<string, unknown> {
    return { code: this.code, message: this.message, details: this.details };
  }
}

export class ValidationError extends ApplicationError {
  constructor(message: string, details?: ErrorDetails) {
    super("validation_error", message, { status: 400, details });
    this.name = "ValidationError";
  }
}

export class AuthorizationError extends ApplicationError {
  constructor(message = "Permission denied", details?: ErrorDetails) {
    super("authorization_error", message, { status: 403, details });
    this.name = "AuthorizationError";
  }
}

export class NotFoundError extends ApplicationError {
  constructor(message: string, details?: ErrorDetails) {
    super("not_found", message, { status: 404, details });
    this.name = "NotFoundError";
  }
}
