# Frappe Assistant Core - AI Assistant integration for Frappe Framework
# Copyright (C) 2026 Paul Clinton
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

import frappe
from frappe.model.document import Document

class FACContextFile(Document):
    def validate(self):
        """Validate context file settings."""
        if self.visibility == "Shared" and not self.shared_with_roles:
            frappe.throw(frappe._("Please specify roles to share with when visibility is 'Shared'"))
