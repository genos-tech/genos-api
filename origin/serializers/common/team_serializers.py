from rest_framework import serializers

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services import blind_index, crypto


class TeamMasterSerializer(serializers.ModelSerializer):
    profile_image_file = serializers.FileField(required=False, allow_empty_file=True)

    class Meta:
        model = TeamMaster
        fields = "__all__"
        # DRF's own unique check on `team_email` filters on the column,
        # encrypted from MIP-619's second release, so `validate_team_email`
        # asks the blind index. Listing validators replaces the model
        # field's, so its lookalike check is listed again.
        # (`team_email_hash` is `editable=False`, so "__all__" never
        # exposes it.)
        extra_kwargs = {"team_email": {"validators": [crypto.refuse_ciphertext_lookalike]}}

    def validate_team_email(self, value):
        others = TeamMaster.objects.filter(blind_index.TEAM_EMAIL.matches(value))
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError(
                "team master with this team email already exists.", code="unique"
            )
        return value


class TeamMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = TeamMembers
        fields = "__all__"
