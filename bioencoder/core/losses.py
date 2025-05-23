import torch.nn as nn
import torch
from pytorch_metric_learning import losses
from dendropy import Tree
from itertools import combinations_with_replacement
import math

class SupConLoss(nn.Module):
    """
    Computes the Supervised Contrastive Loss as described in the paper
    "Supervised Contrastive Learning" (https://arxiv.org/pdf/2004.11362.pdf)
    and supports the unsupervised contrastive loss in SimCLR.

    The contrastive loss encourages the embeddings to be close to their positive
    samples and far away from negative samples. It measures the similarity between
    two samples by the dot product of their embeddings and apply a temperature
    scaling.

    Args:
        temperature (float, optional): The temperature scaling.Default: `0.07`.
        contrast_mode (str, optional): Specifies the mode to compute contrastive loss.
            There are two modes: `all` and `one`. In `all` mode, every sample is used
            as an anchor. In `one` mode, only the first is used as an anchor.
            Default: `'all'`.
        base_temperature (float, optional): The base temperature used to normalize the
            temperature. Default: `0.07`.
    """

    def __init__(self, temperature=0.07, contrast_mode="all", base_temperature=0.07, tree_path=None):
        super(SupConLoss, self).__init__()
        self.temperature = temperature
        self.contrast_mode = contrast_mode
        self.base_temperature = base_temperature
        self.tree_path = tree_path

        if self.tree_path is not None:
            tree = Tree.get(path=tree_path, schema="newick")
            tree.is_rooted = True
            root = tree.seed_node
            tip_depths = {
                leaf.taxon.label: leaf.distance_from_root()
                for leaf in tree.leaf_node_iter()
            }

            ### This makes is so that the labels match the actual dataset labels
            self.tips = sorted(list(tip_depths.keys()))
            n_tips = len(self.tips)

            self.bm_corr = torch.eye(n_tips, dtype=torch.float32)
            ### avoiding redundant correlation checking for the symetric correlation matrix
            for i, j in combinations_with_replacement(range(n_tips), 2):
                if i == j:
                    pass # we've already set the diagonal to 1.0
                elif self.tips[i] == self.tips[j]:
                    raise ValueError(f"Duplicate tip labels found in the tree: {tips[i]}")
                else:
                    anc = tree.mrca(taxon_labels=[self.tips[i], self.tips[j]])
                    if anc is None:
                        raise ValueError(f"Tips {self.tips[i]} and {self.tips[j]} do not share an ancestor in the tree.")
                    elif anc is root:
                        self.bm_corr[i, j] = self.bm_corr[j, i] = 0.0 # if the tips don't share an ancestor until the root of the tree, their bm correlation must be 0.0
                    else:
                        bm_var = anc.distance_from_root()
                        t1_bm_var = tip_depths[self.tips[i]]
                        t2_bm_var = tip_depths[self.tips[j]]
                        self.bm_corr[i, j] = self.bm_corr[j, i] = bm_var / math.sqrt(t1_bm_var * t2_bm_var)


    def forward(self, features, labels=None, mask=None):
        """Compute loss for model. If both `labels` and `mask` are None,
        it degenerates to SimCLR unsupervised loss:
        https://arxiv.org/pdf/2002.05709.pdf

        Args:
            features: hidden vector of shape [bsz, n_views, ...].
            labels: ground truth of shape [bsz].
            mask: contrastive mask of shape [bsz, bsz], mask_{i,j}=1 if sample j
                has the same class as sample i. Can be asymmetric.
        Returns:
            A loss scalar.
        """
        device = torch.device("cuda") if features.is_cuda else torch.device("cpu")

        if len(features.shape) < 3:
            raise ValueError(
                "`features` needs to be [bsz, n_views, ...],"
                "at least 3 dimensions are required"
            )
        if len(features.shape) > 3:
            features = features.view(features.shape[0], features.shape[1], -1)

        batch_size = features.shape[0]
        if labels is not None and mask is not None:
            raise ValueError("Cannot define both `labels` and `mask`")
        elif labels is None and mask is None:
            mask = torch.eye(batch_size, dtype=torch.float32).to(device)
            ### the phylogenetic correlations are introduced into the mask here
        elif labels is not None and self.tree_path is not None:
            mask = torch.eye(batch_size, dtype=torch.float32).to(device)
            for i, j in combinations_with_replacement(range(batch_size), 2):
                if i != j:
                    tip1 = labels[i]
                    tip2 = labels[j]
                    t12_corr = self.bm_corr[tip1, tip2]
                    mask[i, j] = mask[j, i] = t12_corr
        elif labels is not None:
            labels = labels.contiguous().view(-1, 1)
            if labels.shape[0] != batch_size:
                raise ValueError("Num of labels does not match num of features")
            mask = torch.eq(labels, labels.T).float().to(device)
        else:
            mask = mask.float().to(device)

        #### from here
        # essentially concatenating all the different views into one embedding vector for each member of a batch
        contrast_count = features.shape[1]
        contrast_feature = torch.cat(torch.unbind(features, dim=1), dim=0)
        if self.contrast_mode == "one":
            anchor_feature = features[:, 0]
            anchor_count = 1
        elif self.contrast_mode == "all":
            anchor_feature = contrast_feature
            anchor_count = contrast_count
        else:
            raise ValueError("Unknown mode: {}".format(self.contrast_mode))
        

        # compute logits
        anchor_dot_contrast = torch.div(
            torch.matmul(anchor_feature, contrast_feature.T), self.temperature
        )
        # for numerical stability
        logits_max, _ = torch.max(anchor_dot_contrast, dim=1, keepdim=True)
        logits = anchor_dot_contrast - logits_max.detach()

        # tile mask
        mask = mask.repeat(anchor_count, contrast_count)
        # mask-out self-contrast cases
        logits_mask = torch.scatter(
            torch.ones_like(mask),
            1,
            torch.arange(batch_size * anchor_count).view(-1, 1).to(device),
            0,
        )
        mask = mask * logits_mask

        # compute log_prob
        exp_logits = torch.exp(logits) * logits_mask
        log_prob = logits - torch.log(exp_logits.sum(1, keepdim=True))

        # compute mean of log-likelihood over positive
        mean_log_prob_pos = (mask * log_prob).sum(1) / mask.sum(1)

        # loss
        loss = -(self.temperature / self.base_temperature) * mean_log_prob_pos
        loss = loss.view(anchor_count, batch_size).mean()

        return loss


class LabelSmoothingLoss(nn.Module):
    """
    Implements the Label Smoothing Loss for classification problems.

    Args:
    - classes (int): The number of classes in the classification problem.
    - smoothing (float, optional): The smoothing factor for the target distribution. 
        The default value is 0.
    - dim (int, optional): The dimension along which the loss should be computed. 
        The default value is -1.

    Methods:
    - forward(pred, target): Computes the label smoothing loss between `pred` 
        and `target` tensors.

    """
    def __init__(self, classes, smoothing=0, dim=-1):
        super(LabelSmoothingLoss, self).__init__()
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.cls = classes
        self.dim = dim

    def forward(self, pred:torch.Tensor, target:torch.Tensor):
        if not isinstance(pred, torch.Tensor) or not isinstance(target, torch.Tensor):
            raise TypeError("Inputs must be tensors")
        if pred.shape[0] != target.shape[0]:
            raise ValueError("Input tensors must have the same batch size")
            
        pred = pred.log_softmax(dim=self.dim)
        with torch.no_grad():
            true_dist = torch.zeros_like(pred)
            true_dist.fill_(self.smoothing / (self.cls - 1))
            true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        return torch.mean(torch.sum(-true_dist * pred, dim=self.dim))


LOSSES = {
    "SupCon": SupConLoss,
    "LabelSmoothing": LabelSmoothingLoss,
    "CrossEntropy": nn.CrossEntropyLoss,
    "KLDiv": nn.KLDivLoss,
    'SubCenterArcFace': losses.SubCenterArcFaceLoss,
    'ArcFace': losses.ArcFaceLoss,
}


