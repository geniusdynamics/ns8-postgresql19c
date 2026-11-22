//
// Copyright (C) 2026 Genius Dynamics
// SPDX-License-Identifier: GPL-3.0-or-later
//

// Run a module action and resolve with its output. Validation errors
// reject with { validationErrors: [...] }, aborted tasks with { aborted }.
import to from "await-to-js";

export default {
  methods: {
    runModuleTask(action, data, options = {}) {
      return new Promise((resolve, reject) => {
        const eventId = this.getUuid();
        const root = this.core.$root;
        const cleanup = () => {
          root.$off(`${action}-completed-${eventId}`);
          root.$off(`${action}-aborted-${eventId}`);
          root.$off(`${action}-validation-failed-${eventId}`);
        };
        root.$once(
          `${action}-completed-${eventId}`,
          (taskContext, taskResult) => {
            cleanup();
            resolve(taskResult.output);
          }
        );
        root.$once(`${action}-aborted-${eventId}`, (taskResult) => {
          cleanup();
          reject({ aborted: taskResult });
        });
        root.$once(
          `${action}-validation-failed-${eventId}`,
          (validationErrors) => {
            cleanup();
            reject({ validationErrors });
          }
        );
        to(
          this.createModuleTaskForApp(this.instanceName, {
            action,
            data: data || {},
            extra: {
              title: options.title || this.$t("action." + action),
              description: options.description || "",
              isNotificationHidden: options.hidden !== false,
              eventId,
            },
          })
        ).then(([err]) => {
          if (err) {
            cleanup();
            reject({ error: this.getErrorMessage(err) });
          }
        });
      });
    },
    // Translate the errors of a rejected runModuleTask() into this.error
    // (field errors) and return a message for the generic notification
    taskErrorMessage(failure) {
      if (failure && failure.validationErrors) {
        let first = "";
        for (const verr of failure.validationErrors) {
          const msg =
            this.$t("validation." + verr.error) +
            (verr.value ? ` (${verr.value})` : "");
          if (verr.parameter in this.error) {
            this.error[verr.parameter] = msg;
          }
          if (!first) {
            first = verr.parameter + ": " + msg;
          }
        }
        return first;
      }
      if (failure && failure.error) {
        return failure.error;
      }
      return this.$t("error.generic_error");
    },
    linesToList(text) {
      return (text || "")
        .split(/[\n,]/)
        .map((s) => s.trim())
        .filter((s) => s);
    },
  },
};
