from rest_framework.response import Response
from django.utils import timezone

class StandardResponseMixin:
    """Mixin for consistent API responses"""
    def success_response(self, data, message="Success", status_code=200):
        return Response({
            "success": True,
            "statusCode": status_code,
            "message": message,
            "data": data,
            "timestamp": timezone.now().isoformat()
        }, status=status_code)
    
    def error_response(self, message, status_code=400, data=None):
        detail = self.extract_first_error(data)
        if detail and detail not in message:
            message = f"{message}: {detail}"
        return Response({
            "success": False,
            "statusCode": status_code,
            "message": message,
            "data": data,
            "timestamp": timezone.now().isoformat()
        }, status=status_code)

    def extract_first_error(self, errors):
        if isinstance(errors, dict):
            if "non_field_errors" in errors:
                extracted = self.extract_first_error(errors.get("non_field_errors"))
                if extracted:
                    return extracted
            for field, value in errors.items():
                if isinstance(value, list) and value:
                    nested = value[0]
                    if isinstance(nested, (dict, list)):
                        extracted = self.extract_first_error(nested)
                        if extracted:
                            return f"{field} :: {extracted}"
                    return f"{field} :: {nested}"
                if isinstance(value, str):
                    return f"{field} :: {value}"
                extracted = self.extract_first_error(value)
                if extracted:
                    return f"{field} :: {extracted}"
        elif isinstance(errors, list) and errors:
            extracted = self.extract_first_error(errors[0])
            if extracted:
                return extracted
        elif errors:
            return str(errors)
        return None

def extract_first_error(errors):
    """Extract the first error message from serializer errors dict"""
    for field, messages in errors.items():
        if isinstance(messages, list) and messages:
            return messages[0]
        elif isinstance(messages, str):
            return messages
    return "Validation Error"
